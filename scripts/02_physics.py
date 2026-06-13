from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


def main() -> int:
    con = duckdb.connect(str(config.DB_PATH))
    con.execute("PRAGMA threads=4")
    con.execute("PRAGMA memory_limit='10GB'")

    gamma = config.TEMP_COEFFICIENT_PER_C

    print("Computing daily altitude-peak timing...")
    altitude_peak = con.execute(
        """
        WITH daily AS (
          SELECT
            CAST(ts AS DATE) AS d,
            ts,
            altitude_deg,
            row_number() OVER (PARTITION BY CAST(ts AS DATE) ORDER BY altitude_deg DESC) AS rn
          FROM plant
          WHERE altitude_deg IS NOT NULL
            AND ts >= TIMESTAMP '2018-06-01'
            AND ts < TIMESTAMP '2018-07-01'
        )
        SELECT
          avg(extract(hour FROM ts) * 60 + extract(minute FROM ts)) AS avg_peak_minute,
          min(ts) AS first_peak,
          max(ts) AS last_peak,
          avg(altitude_deg) AS avg_max_altitude
        FROM daily
        WHERE rn = 1
        """
    ).fetchdf().iloc[0].to_dict()

    print("Fitting PVWatts-style eta factors per inverter...")
    con.execute("DROP TABLE IF EXISTS physics_eta")
    con.execute(
        f"""
        CREATE TABLE physics_eta AS
        WITH base AS (
          SELECT
            r.inverter,
            r.p_ac_kw,
            i.kwp,
            p.irradiation_w_m2,
            COALESCE(p.module_temp_c, p.ambient_temp_c, 25.0) AS temp_c,
            i.kwp * (p.irradiation_w_m2 / 1000.0)
              * GREATEST(0.65, 1.0 + ({gamma}) * (COALESCE(p.module_temp_c, p.ambient_temp_c, 25.0) - 25.0))
              AS raw_expected_kw
          FROM readings r
          JOIN inverters i USING (inverter)
          JOIN plant p USING (ts)
          WHERE r.ts >= TIMESTAMP '2017-01-01'
            AND r.ts < TIMESTAMP '2018-01-01'
            AND p.altitude_deg > {config.DAY_ALTITUDE_DEG}
            AND p.irradiation_w_m2 > 200
            AND COALESCE(p.is_curtailed, false) = false
            AND r.p_ac_kw IS NOT NULL
            AND r.p_ac_kw > 0
        )
        SELECT
          inverter,
          median(p_ac_kw / NULLIF(raw_expected_kw, 0)) AS eta_system,
          count(*) AS n_train_points
        FROM base
        WHERE raw_expected_kw > 0
          AND p_ac_kw / NULLIF(raw_expected_kw, 0) BETWEEN 0.2 AND 1.2
        GROUP BY inverter
        """
    )

    print("Creating physics prediction table...")
    con.execute("DROP TABLE IF EXISTS physics_predictions")
    con.execute(
        f"""
        CREATE TABLE physics_predictions AS
        SELECT
          r.ts,
          r.inverter,
          GREATEST(
            0.0,
            LEAST(
              i.kwp,
              i.kwp * (p.irradiation_w_m2 / 1000.0)
                * GREATEST(0.65, 1.0 + ({gamma}) * (COALESCE(p.module_temp_c, p.ambient_temp_c, 25.0) - 25.0))
                * COALESCE(e.eta_system, 0.82)
            )
          ) AS p50_physics_kw,
          GREATEST(
            0.0,
            LEAST(
              i.kwp,
              i.kwp * (p.irradiation_w_m2 / 1000.0)
                * GREATEST(0.65, 1.0 + ({gamma}) * (COALESCE(p.module_temp_c, p.ambient_temp_c, 25.0) - 25.0))
                * COALESCE(e.eta_system, 0.82)
                * 0.90
            )
          ) AS p10_physics_kw,
          GREATEST(
            0.0,
            LEAST(
              i.kwp,
              i.kwp * (p.irradiation_w_m2 / 1000.0)
                * GREATEST(0.65, 1.0 + ({gamma}) * (COALESCE(p.module_temp_c, p.ambient_temp_c, 25.0) - 25.0))
                * COALESCE(e.eta_system, 0.82)
                * 1.10
            )
          ) AS p90_physics_kw,
          e.eta_system,
          CASE
            WHEN f.fleet_median_kw_per_kwp IS NULL OR f.fleet_median_kw_per_kwp = 0 THEN NULL
            ELSE (r.p_ac_kw / NULLIF(i.kwp, 0)) / f.fleet_median_kw_per_kwp
          END AS peer_ratio
        FROM readings r
        JOIN inverters i USING (inverter)
        JOIN plant p USING (ts)
        LEFT JOIN physics_eta e USING (inverter)
        LEFT JOIN fleet_5min f USING (ts)
        """
    )

    print("Computing monthly performance ratio...")
    con.execute("DROP TABLE IF EXISTS performance_ratio_monthly")
    con.execute(
        """
        CREATE TABLE performance_ratio_monthly AS
        SELECT
          date_trunc('month', r.ts) AS month,
          r.inverter,
          sum(GREATEST(r.p_ac_kw, 0) * (5.0 / 60.0)) AS actual_energy_kwh,
          sum(i.kwp * (p.irradiation_w_m2 / 1000.0) * (5.0 / 60.0)) AS reference_energy_kwh,
          actual_energy_kwh / NULLIF(reference_energy_kwh, 0) AS performance_ratio
        FROM readings r
        JOIN inverters i USING (inverter)
        JOIN plant p USING (ts)
        WHERE p.is_day
          AND p.irradiation_w_m2 > 20
          AND r.p_ac_kw IS NOT NULL
        GROUP BY 1, 2
        """
    )

    print("Creating monthly peer/deviation helper table...")
    con.execute("DROP TABLE IF EXISTS physics_monthly")
    con.execute(
        """
        CREATE TABLE physics_monthly AS
        SELECT
          date_trunc('month', r.ts) AS month,
          r.inverter,
          sum(GREATEST(r.p_ac_kw, 0) * (5.0 / 60.0)) AS actual_energy_kwh,
          sum(GREATEST(pp.p50_physics_kw, 0) * (5.0 / 60.0)) AS physics_expected_kwh,
          sum(GREATEST(pp.p50_physics_kw - COALESCE(r.p_ac_kw, 0), 0) * (5.0 / 60.0)) AS positive_shortfall_kwh,
          median(pp.peer_ratio) FILTER (WHERE p.is_day AND p.irradiation_w_m2 > 50) AS median_peer_ratio,
          avg(pp.peer_ratio) FILTER (WHERE p.is_day AND p.irradiation_w_m2 > 50) AS avg_peer_ratio
        FROM readings r
        JOIN physics_predictions pp USING (ts, inverter)
        JOIN plant p USING (ts)
        GROUP BY 1, 2
        """
    )

    print("Gathering metrics...")
    counts = {
        "physics_eta": con.execute("SELECT COUNT(*) FROM physics_eta").fetchone()[0],
        "physics_predictions": con.execute("SELECT COUNT(*) FROM physics_predictions").fetchone()[0],
        "performance_ratio_monthly": con.execute("SELECT COUNT(*) FROM performance_ratio_monthly").fetchone()[0],
        "physics_monthly": con.execute("SELECT COUNT(*) FROM physics_monthly").fetchone()[0],
    }
    eta_stats = con.execute(
        """
        SELECT
          min(eta_system) AS min_eta,
          median(eta_system) AS median_eta,
          max(eta_system) AS max_eta,
          min(n_train_points) AS min_train_points,
          median(n_train_points) AS median_train_points,
          max(n_train_points) AS max_train_points
        FROM physics_eta
        """
    ).fetchdf().iloc[0].to_dict()
    pr_stats = con.execute(
        """
        SELECT
          min(performance_ratio) FILTER (WHERE reference_energy_kwh > 100) AS min_pr,
          median(performance_ratio) FILTER (WHERE reference_energy_kwh > 100) AS median_pr,
          max(performance_ratio) FILTER (WHERE reference_energy_kwh > 100) AS max_pr
        FROM performance_ratio_monthly
        """
    ).fetchdf().iloc[0].to_dict()
    peer_stats = con.execute(
        """
        SELECT
          min(median_peer_ratio) AS min_peer,
          median(median_peer_ratio) AS median_peer,
          max(median_peer_ratio) AS max_peer
        FROM physics_monthly
        WHERE median_peer_ratio IS NOT NULL
        """
    ).fetchdf().iloc[0].to_dict()

    metrics = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {k: int(v) for k, v in counts.items()},
        "altitude_peak_june_2018": {
            k: (None if v is None else (float(v) if isinstance(v, (int, float)) else str(v)))
            for k, v in altitude_peak.items()
        },
        "eta_stats": {k: (None if v is None else float(v)) for k, v in eta_stats.items()},
        "performance_ratio_stats": {k: (None if v is None else float(v)) for k, v in pr_stats.items()},
        "peer_ratio_stats": {k: (None if v is None else float(v)) for k, v in peer_stats.items()},
        "temperature_coefficient_per_c": gamma,
    }
    (config.OUTPUT_DIR / "physics_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    avg_peak_min = altitude_peak["avg_peak_minute"]
    avg_peak_str = f"{int(avg_peak_min // 60):02d}:{int(avg_peak_min % 60):02d}" if avg_peak_min else "n/a"
    lines = [
        "# SolarTwin Phase 2 Physics Baseline Summary",
        "",
        f"Generated: `{metrics['generated_at']}`",
        "",
        "## Tables Created",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ]
    for table, count in counts.items():
        lines.append(f"| `{table}` | {count:,} |")
    lines += [
        "",
        "## Baseline Formula",
        "",
        "`P_expected = kWp × (irradiation / 1000) × [1 + gamma × (module_temperature - 25)] × eta_system`",
        "",
        f"- Temperature coefficient `gamma`: `{gamma}` per °C.",
        "- `eta_system` is fitted per inverter from non-curtailed, high-irradiance daylight points in 2017.",
        "- The baseline is clipped at `0 <= P_expected <= kWp`.",
        "",
        "## Eta Fit",
        "",
        f"- Eta median: `{eta_stats['median_eta']:.3f}`",
        f"- Eta range: `{eta_stats['min_eta']:.3f}` to `{eta_stats['max_eta']:.3f}`",
        f"- Median training points per inverter: `{eta_stats['median_train_points']:.0f}`",
        "",
        "## Performance Ratio",
        "",
        f"- Median monthly PR: `{pr_stats['median_pr']:.3f}`",
        f"- Monthly PR range for non-trivial months: `{pr_stats['min_pr']:.3f}` to `{pr_stats['max_pr']:.3f}`",
        "",
        "## Peer Normalization",
        "",
        f"- Median monthly peer ratio: `{peer_stats['median_peer']:.3f}`",
        f"- Monthly peer-ratio range: `{peer_stats['min_peer']:.3f}` to `{peer_stats['max_peer']:.3f}`",
        "",
        "## Timestamp / Altitude Note",
        "",
        f"- In June 2018 the daily altitude maximum occurs at about `{avg_peak_str}` in the source timestamps.",
        "- This is consistent with UTC-like timestamps for a German plant during summer daylight-saving time.",
        "- The MVP keeps timestamps as provided; the measured irradiation/temperature baseline does not require absolute timezone conversion.",
    ]
    (config.OUTPUT_DIR / "physics_summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
