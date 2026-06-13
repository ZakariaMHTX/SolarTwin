from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


GRID_PRICE_THRESHOLD_PCT = 99.5
MIN_LOSS_KW = 0.02
# DATA_GAP corroboration: a missing reading is only a real OUTAGE if the plant-level
# Janitza meter does NOT show the plant producing in line with the reporting fleet.
DATA_GAP_JANITZA_RATIO = 0.7
# Persistence gate for UNDERPERFORMANCE_LOCAL: with p10/p90 band coverage ~0.80,
# ~10% of healthy intervals fall below p10 by chance. Require >=9 of the 13
# intervals in a +/-30 min window below p10 so isolated statistical dips are dropped.
PERSISTENCE_MIN_COUNT = 9


def sql_path(path: Path) -> str:
    return str(path).replace("'", "''")


def create_context_tables(con: duckdb.DuckDBPyConnection) -> None:
    print("Creating canonical plant controls...")
    con.execute("DROP TABLE IF EXISTS plant_controls")
    con.execute(
        """
        CREATE TABLE plant_controls AS
        SELECT
          ts,
          avg(COALESCE(dv_pct, 100.0)) AS dv_pct,
          avg(COALESCE(evu_pct, 100.0)) AS evu_pct,
          bool_or(COALESCE(is_curtailed, false)) AS is_curtailed,
          avg(-janitza_p_ac_kw) AS plant_feed_in_kw
        FROM plant
        GROUP BY ts
        """
    )

    print("Creating +/-15 minute error context...")
    con.execute("DROP TABLE IF EXISTS error_context_15min")
    con.execute(
        """
        CREATE TABLE error_context_15min AS
        WITH offsets(offset_min) AS (
          VALUES (-15), (-10), (-5), (0), (5), (10), (15)
        ),
        expanded AS (
          SELECT
            e.ts + offset_min * INTERVAL '1 minute' AS ts,
            e.inverter,
            min(e.error_code) AS error_code,
            count(*) AS n_error_rows
          FROM error_intervals e
          CROSS JOIN offsets
          GROUP BY 1, 2
        )
        SELECT
          ex.ts,
          ex.inverter,
          ex.error_code,
          ex.n_error_rows,
          ex.error_code % 65536 AS error_code_base,
          ec.description AS error_description,
          ec.severity_class AS error_severity
        FROM expanded ex
        JOIN twin_features tf USING (ts)
        LEFT JOIN error_catalog ec ON ec.code = ex.error_code % 65536
        """
    )


def create_loss_intervals(con: duckdb.DuckDBPyConnection, total_kwp: float) -> None:
    print("Classifying interval-level losses...")
    con.execute("DROP TABLE IF EXISTS loss_intervals")
    con.execute(
        f"""
        CREATE TABLE loss_intervals AS
        WITH base AS (
          SELECT
            r.ts,
            r.inverter,
            EXTRACT(year FROM r.ts)::INTEGER AS year,
            date_trunc('month', r.ts)::DATE AS month,
            i.module_type,
            i.kwp,
            GREATEST(COALESCE(r.p_ac_kw, 0.0), 0.0) AS actual_kw,
            r.p_ac_kw IS NULL AS actual_missing,
            tp.p10_twin_kw,
            tp.p50_twin_kw,
            tp.p90_twin_kw,
            tf.irradiation_w_m2,
            pc.dv_pct,
            pc.evu_pct,
            pc.plant_feed_in_kw,
            COALESCE(tm.ct_per_kwh, 10.0) AS ct_per_kwh,
            ec.error_code,
            ec.error_code_base,
            ec.error_description,
            ec.error_severity,
            f.fleet_median_kw_per_kwp,
            CASE
              WHEN f.fleet_median_kw_per_kwp IS NULL OR f.fleet_median_kw_per_kwp = 0 THEN NULL
              ELSE (GREATEST(COALESCE(r.p_ac_kw, 0.0), 0.0) / NULLIF(i.kwp, 0))
                / f.fleet_median_kw_per_kwp
            END AS peer_ratio
          FROM readings r
          JOIN twin_predictions tp USING (ts, inverter)
          JOIN twin_features tf USING (ts)
          JOIN inverters i USING (inverter)
          LEFT JOIN plant_controls pc USING (ts)
          LEFT JOIN error_context_15min ec USING (ts, inverter)
          LEFT JOIN fleet_5min f USING (ts)
          LEFT JOIN tariffs_monthly tm
            ON tm.inverter = r.inverter
           AND tm.month = date_trunc('month', r.ts)
          WHERE tf.is_model_day
            AND tp.p50_twin_kw > 0
        ),
        flagged AS (
          SELECT
            *,
            (NOT actual_missing AND actual_kw < p10_twin_kw) AS below_p10,
            sum(CASE WHEN (NOT actual_missing AND actual_kw < p10_twin_kw) THEN 1 ELSE 0 END)
              OVER (
                PARTITION BY inverter
                ORDER BY ts
                RANGE BETWEEN INTERVAL '30 minutes' PRECEDING
                          AND INTERVAL '30 minutes' FOLLOWING
              ) AS below_p10_count_1h
          FROM base
        ),
        candidates AS (
          SELECT
            *,
            CASE
              WHEN evu_pct < {GRID_PRICE_THRESHOLD_PCT} THEN 'CURTAILMENT_GRID'
              WHEN dv_pct < {GRID_PRICE_THRESHOLD_PCT} THEN 'CURTAILMENT_PRICE'
              WHEN error_code IS NOT NULL THEN 'FAULT'
              WHEN actual_missing
                AND COALESCE(fleet_median_kw_per_kwp, 0) > 0.10
                AND (
                  plant_feed_in_kw IS NULL
                  OR plant_feed_in_kw
                     > {DATA_GAP_JANITZA_RATIO} * fleet_median_kw_per_kwp * {total_kwp}
                ) THEN 'DATA_GAP'
              WHEN (actual_missing OR actual_kw <= 0.01 * kwp)
                AND COALESCE(fleet_median_kw_per_kwp, 0) > 0.10 THEN 'OUTAGE'
              WHEN below_p10 AND below_p10_count_1h >= {PERSISTENCE_MIN_COUNT}
                THEN 'UNDERPERFORMANCE_LOCAL'
              ELSE NULL
            END AS bucket,
            CASE
              WHEN evu_pct < {GRID_PRICE_THRESHOLD_PCT}
                THEN LEAST(actual_kw, GREATEST(0.0, evu_pct / 100.0 * kwp))
              WHEN dv_pct < {GRID_PRICE_THRESHOLD_PCT}
                THEN LEAST(actual_kw, GREATEST(0.0, dv_pct / 100.0 * kwp))
              ELSE actual_kw
            END AS effective_actual_kw
          FROM flagged
          WHERE
            (
              (COALESCE(evu_pct, 100.0) < {GRID_PRICE_THRESHOLD_PCT} AND actual_kw < p50_twin_kw)
              OR (COALESCE(dv_pct, 100.0) < {GRID_PRICE_THRESHOLD_PCT} AND actual_kw < p50_twin_kw)
              OR actual_kw < p10_twin_kw
              OR actual_missing
            )
        )
        SELECT
          ts,
          inverter,
          year,
          month,
          module_type,
          bucket,
          error_code,
          error_code_base,
          error_description,
          error_severity,
          kwp,
          actual_kw,
          effective_actual_kw,
          p10_twin_kw,
          p50_twin_kw,
          p90_twin_kw,
          irradiation_w_m2,
          dv_pct,
          evu_pct,
          peer_ratio,
          fleet_median_kw_per_kwp,
          ct_per_kwh,
          GREATEST(p50_twin_kw - effective_actual_kw, 0.0) AS lost_kw,
          GREATEST(p50_twin_kw - effective_actual_kw, 0.0) * (5.0 / 60.0) AS lost_kwh,
          GREATEST(p50_twin_kw - effective_actual_kw, 0.0) * (5.0 / 60.0)
            * (ct_per_kwh / 100.0) AS lost_eur
        FROM candidates
        WHERE bucket IS NOT NULL
          AND GREATEST(p50_twin_kw - effective_actual_kw, 0.0) > {MIN_LOSS_KW}
        """
    )


def create_ledger_tables(con: duckdb.DuckDBPyConnection) -> None:
    print("Aggregating raw interval ledger...")
    con.execute("DROP TABLE IF EXISTS interval_ledger_raw")
    con.execute(
        """
        CREATE TABLE interval_ledger_raw AS
        SELECT
          inverter,
          year,
          month,
          module_type,
          bucket,
          error_code,
          error_code_base,
          any_value(error_description) AS error_description,
          sum(lost_kwh) AS lost_kwh,
          sum(lost_eur) AS lost_eur,
          count(*) AS n_intervals,
          avg(ct_per_kwh) AS avg_ct_per_kwh
        FROM loss_intervals
        GROUP BY 1, 2, 3, 4, 5, 6, 7
        """
    )

    print("Estimating monthly degradation share...")
    con.execute("DROP TABLE IF EXISTS twin_expected_monthly")
    con.execute(
        """
        CREATE TABLE twin_expected_monthly AS
        SELECT
          date_trunc('month', tp.ts)::DATE AS month,
          tp.inverter,
          any_value(i.module_type) AS module_type,
          sum(GREATEST(tp.p50_twin_kw, 0) * (5.0 / 60.0)) AS expected_daylight_kwh
        FROM twin_predictions tp
        JOIN twin_features tf USING (ts)
        JOIN inverters i USING (inverter)
        WHERE tf.is_model_day
        GROUP BY 1, 2
        """
    )

    con.execute("DROP TABLE IF EXISTS underperformance_monthly_raw")
    con.execute(
        """
        CREATE TABLE underperformance_monthly_raw AS
        SELECT
          inverter,
          month,
          sum(lost_kwh) AS underperformance_lost_kwh,
          sum(lost_eur) AS underperformance_lost_eur,
          avg(avg_ct_per_kwh) AS avg_ct_per_kwh
        FROM interval_ledger_raw
        WHERE bucket = 'UNDERPERFORMANCE_LOCAL'
        GROUP BY 1, 2
        """
    )

    con.execute("DROP TABLE IF EXISTS degradation_monthly_loss")
    con.execute(
        """
        CREATE TABLE degradation_monthly_loss AS
        WITH trend AS (
          SELECT
            e.month,
            EXTRACT(year FROM e.month)::INTEGER AS year,
            e.inverter,
            e.module_type,
            e.expected_daylight_kwh,
            ds.slope_ratio_per_year,
            GREATEST(
              0.0,
              LEAST(
                0.40,
                -ds.slope_ratio_per_year
                  * GREATEST(0.0, date_diff('day', CAST(ds.first_month AS DATE), e.month) / 365.25)
              )
            ) AS degradation_fraction
          FROM twin_expected_monthly e
          JOIN degradation_slopes ds USING (inverter)
          WHERE e.month >= CAST(ds.first_month AS DATE)
        ),
        raw AS (
          SELECT
            t.*,
            t.expected_daylight_kwh * t.degradation_fraction AS raw_degradation_kwh,
            COALESCE(u.underperformance_lost_kwh, 0.0) AS underperformance_lost_kwh,
            COALESCE(u.avg_ct_per_kwh, tm.ct_per_kwh, 10.0) AS ct_per_kwh
          FROM trend t
          LEFT JOIN underperformance_monthly_raw u USING (inverter, month)
          LEFT JOIN tariffs_monthly tm ON tm.inverter = t.inverter AND tm.month = t.month
        )
        -- Degradation is a systematic trend measured from the twin residual slope.
        -- It is NOT capped at the below-p10 underperformance energy: most degradation
        -- loss hides inside the p10..p90 band. Overlap with UNDERPERFORMANCE_LOCAL
        -- rows is subtracted in interval_ledger_adjusted, so nothing double-counts.
        SELECT
          inverter,
          year,
          month,
          module_type,
          raw_degradation_kwh AS lost_kwh,
          raw_degradation_kwh * (ct_per_kwh / 100.0) AS lost_eur,
          ct_per_kwh,
          degradation_fraction,
          raw_degradation_kwh,
          underperformance_lost_kwh
        FROM raw
        WHERE raw_degradation_kwh > 0.001
        """
    )

    print("Splitting degradation out of local underperformance...")
    con.execute("DROP TABLE IF EXISTS interval_ledger_adjusted")
    con.execute(
        """
        CREATE TABLE interval_ledger_adjusted AS
        SELECT
          r.inverter,
          r.year,
          r.month,
          r.module_type,
          r.bucket,
          r.error_code,
          r.error_code_base,
          r.error_description,
          CASE
            WHEN r.bucket = 'UNDERPERFORMANCE_LOCAL'
              THEN GREATEST(r.lost_kwh - COALESCE(d.lost_kwh, 0.0), 0.0)
            ELSE r.lost_kwh
          END AS lost_kwh,
          CASE
            WHEN r.bucket = 'UNDERPERFORMANCE_LOCAL'
              THEN GREATEST(r.lost_eur - COALESCE(d.lost_eur, 0.0), 0.0)
            ELSE r.lost_eur
          END AS lost_eur,
          r.n_intervals,
          r.avg_ct_per_kwh
        FROM interval_ledger_raw r
        LEFT JOIN degradation_monthly_loss d
          ON d.inverter = r.inverter
         AND d.month = r.month
         AND r.bucket = 'UNDERPERFORMANCE_LOCAL'
        WHERE CASE
            WHEN r.bucket = 'UNDERPERFORMANCE_LOCAL'
              THEN GREATEST(r.lost_kwh - COALESCE(d.lost_kwh, 0.0), 0.0)
            ELSE r.lost_kwh
          END > 0.001
        """
    )

    print("Building final ticket-validated ledger...")
    con.execute("DROP TABLE IF EXISTS ledger_unvalidated")
    con.execute(
        """
        CREATE TABLE ledger_unvalidated AS
        SELECT
          inverter,
          year,
          month,
          module_type,
          bucket,
          error_code,
          error_code_base,
          error_description,
          lost_kwh,
          lost_eur,
          n_intervals,
          avg_ct_per_kwh
        FROM interval_ledger_adjusted
        UNION ALL
        SELECT
          inverter,
          year,
          month,
          module_type,
          'DEGRADATION' AS bucket,
          NULL::BIGINT AS error_code,
          NULL::BIGINT AS error_code_base,
          NULL::VARCHAR AS error_description,
          lost_kwh,
          lost_eur,
          0::BIGINT AS n_intervals,
          ct_per_kwh AS avg_ct_per_kwh
        FROM degradation_monthly_loss
        """
    )

    con.execute("DROP TABLE IF EXISTS ledger")
    con.execute(
        """
        CREATE TABLE ledger AS
        SELECT
          l.inverter,
          l.year,
          l.month,
          l.module_type,
          l.bucket,
          l.error_code,
          l.error_code_base,
          l.error_description,
          l.lost_kwh,
          l.lost_eur,
          l.n_intervals,
          l.avg_ct_per_kwh,
          EXISTS (
            SELECT 1
            FROM tickets t
            WHERE t.start_ts IS NOT NULL
              AND t.start_ts >= l.month - INTERVAL '14 days'
              AND t.start_ts < l.month + INTERVAL '1 month' + INTERVAL '14 days'
              AND (
                t.inverter = l.inverter
                OR (
                  t.inverter IS NULL
                  AND l.bucket = 'CURTAILMENT_GRID'
                  AND lower(COALESCE(t.text, '')) LIKE '%netz%'
                )
              )
          ) AS validated_by_ticket
        FROM ledger_unvalidated l
        WHERE l.lost_kwh > 0.001
        """
    )


def create_rollups_and_exports(con: duckdb.DuckDBPyConnection) -> None:
    print("Creating rollup views and CSV exports...")
    con.execute("DROP VIEW IF EXISTS ledger_by_year_cause")
    con.execute(
        """
        CREATE VIEW ledger_by_year_cause AS
        SELECT
          year,
          bucket,
          sum(lost_kwh) AS lost_kwh,
          sum(lost_eur) AS lost_eur,
          sum(n_intervals) AS n_intervals
        FROM ledger
        GROUP BY 1, 2
        ORDER BY year, lost_eur DESC
        """
    )

    con.execute("DROP VIEW IF EXISTS ledger_top20_inverter_cause")
    con.execute(
        """
        CREATE VIEW ledger_top20_inverter_cause AS
        SELECT
          inverter,
          bucket,
          sum(lost_kwh) AS lost_kwh,
          sum(lost_eur) AS lost_eur,
          sum(n_intervals) AS n_intervals
        FROM ledger
        GROUP BY 1, 2
        ORDER BY lost_eur DESC
        LIMIT 20
        """
    )

    con.execute("DROP VIEW IF EXISTS ledger_module_type_year")
    con.execute(
        """
        CREATE VIEW ledger_module_type_year AS
        SELECT
          year,
          module_type,
          bucket,
          sum(lost_kwh) AS lost_kwh,
          sum(lost_eur) AS lost_eur
        FROM ledger
        GROUP BY 1, 2, 3
        ORDER BY year, module_type, lost_eur DESC
        """
    )

    exports = {
        "ledger.csv": "SELECT * FROM ledger ORDER BY lost_eur DESC",
        "ledger_by_year_cause.csv": "SELECT * FROM ledger_by_year_cause",
        "ledger_top20_inverter_cause.csv": "SELECT * FROM ledger_top20_inverter_cause",
        "ledger_module_type_year.csv": "SELECT * FROM ledger_module_type_year",
    }
    for filename, query in exports.items():
        out_path = config.OUTPUT_DIR / filename
        con.execute(f"COPY ({query}) TO '{sql_path(out_path)}' (HEADER, DELIMITER ',')")


def create_ticket_validation_events(con: duckdb.DuckDBPyConnection) -> None:
    print("Creating top ticket-validation events...")
    con.execute("DROP TABLE IF EXISTS ticket_validation_events")
    con.execute(
        """
        CREATE TABLE ticket_validation_events AS
        WITH daily AS (
          SELECT
            row_number() OVER (ORDER BY sum(lost_eur) DESC) AS event_id,
            CAST(ts AS DATE) AS event_date,
            min(ts) AS first_ts,
            max(ts) AS last_ts,
            inverter,
            bucket,
            error_code,
            any_value(error_description) AS error_description,
            sum(lost_kwh) AS lost_kwh,
            sum(lost_eur) AS lost_eur,
            count(*) AS n_intervals
          FROM loss_intervals
          WHERE bucket IN ('FAULT', 'OUTAGE', 'UNDERPERFORMANCE_LOCAL')
          GROUP BY CAST(ts AS DATE), inverter, bucket, error_code
          ORDER BY lost_eur DESC
          LIMIT 50
        ),
        matches AS (
          SELECT
            d.*,
            t.start_ts AS ticket_start_ts,
            t.end_ts AS ticket_end_ts,
            t.category AS ticket_category,
            t.text AS ticket_text,
            date_diff('day', d.first_ts, t.start_ts) AS ticket_lag_days,
            row_number() OVER (
              PARTITION BY d.event_id
              ORDER BY abs(date_diff('day', d.first_ts, t.start_ts))
            ) AS rn
          FROM daily d
          LEFT JOIN tickets t
            ON t.inverter = d.inverter
           AND t.start_ts IS NOT NULL
           AND t.start_ts >= d.first_ts - INTERVAL '14 days'
           AND t.start_ts < d.first_ts + INTERVAL '14 days'
        )
        SELECT
          event_id,
          event_date,
          first_ts,
          last_ts,
          inverter,
          bucket,
          error_code,
          error_description,
          lost_kwh,
          lost_eur,
          n_intervals,
          ticket_start_ts,
          ticket_end_ts,
          ticket_category,
          ticket_text,
          ticket_lag_days,
          ticket_start_ts IS NOT NULL AS validated_by_ticket
        FROM matches
        WHERE rn = 1
        ORDER BY lost_eur DESC
        """
    )
    con.execute(
        f"""
        COPY (
          SELECT * FROM ticket_validation_events ORDER BY lost_eur DESC
        ) TO '{sql_path(config.OUTPUT_DIR / "ticket_validation_events.csv")}'
        (HEADER, DELIMITER ',')
        """
    )

    con.execute("DROP TABLE IF EXISTS ticket_lead_events")
    con.execute(
        """
        CREATE TABLE ticket_lead_events AS
        WITH daily AS (
          SELECT
            CAST(ts AS DATE) AS event_date,
            min(ts) AS first_ts,
            max(ts) AS last_ts,
            inverter,
            bucket,
            error_code,
            any_value(error_description) AS error_description,
            sum(lost_kwh) AS lost_kwh,
            sum(lost_eur) AS lost_eur,
            count(*) AS n_intervals
          FROM loss_intervals
          WHERE bucket IN ('FAULT', 'OUTAGE', 'UNDERPERFORMANCE_LOCAL')
          GROUP BY CAST(ts AS DATE), inverter, bucket, error_code
        ),
        matches AS (
          SELECT
            d.*,
            t.start_ts AS ticket_start_ts,
            t.end_ts AS ticket_end_ts,
            t.category AS ticket_category,
            t.text AS ticket_text,
            date_diff('day', d.first_ts, t.start_ts) AS ticket_lag_days,
            row_number() OVER (
              PARTITION BY d.event_date, d.inverter, d.bucket, d.error_code
              ORDER BY t.start_ts
            ) AS rn
          FROM daily d
          JOIN tickets t
            ON t.inverter = d.inverter
           AND t.start_ts IS NOT NULL
           AND t.start_ts >= d.first_ts
           AND t.start_ts < d.first_ts + INTERVAL '30 days'
        ),
        ranked AS (
          SELECT
            row_number() OVER (ORDER BY lost_eur DESC) AS lead_event_id,
            *
          FROM matches
          WHERE rn = 1
        )
        SELECT
          lead_event_id,
          event_date,
          first_ts,
          last_ts,
          inverter,
          bucket,
          error_code,
          error_description,
          lost_kwh,
          lost_eur,
          n_intervals,
          ticket_start_ts,
          ticket_end_ts,
          ticket_category,
          ticket_text,
          ticket_lag_days
        FROM ranked
        ORDER BY lost_eur DESC
        LIMIT 50
        """
    )
    con.execute(
        f"""
        COPY (
          SELECT * FROM ticket_lead_events ORDER BY lost_eur DESC
        ) TO '{sql_path(config.OUTPUT_DIR / "ticket_lead_events.csv")}'
        (HEADER, DELIMITER ',')
        """
    )


def gather_metrics(con: duckdb.DuckDBPyConnection) -> dict:
    counts = {}
    for table in [
        "plant_controls",
        "error_context_15min",
        "loss_intervals",
        "interval_ledger_raw",
        "degradation_monthly_loss",
        "ledger",
        "ticket_validation_events",
        "ticket_lead_events",
    ]:
        counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    totals_by_bucket = con.execute(
        """
        SELECT
          bucket,
          sum(lost_kwh) AS lost_kwh,
          sum(lost_eur) AS lost_eur,
          sum(n_intervals) AS n_intervals,
          count(*) AS ledger_rows
        FROM ledger
        GROUP BY 1
        ORDER BY lost_eur DESC
        """
    ).fetchdf()

    total_loss = con.execute(
        """
        SELECT
          sum(lost_kwh) FILTER (WHERE bucket != 'DATA_GAP') AS lost_kwh,
          sum(lost_eur) FILTER (WHERE bucket != 'DATA_GAP') AS lost_eur,
          sum(lost_kwh) FILTER (WHERE bucket = 'DATA_GAP') AS data_gap_kwh,
          sum(lost_eur) FILTER (WHERE bucket = 'DATA_GAP') AS data_gap_eur,
          count(*) AS ledger_rows,
          count(*) FILTER (WHERE validated_by_ticket) AS validated_rows
        FROM ledger
        """
    ).fetchdf().iloc[0].to_dict()

    # Identity: ledger total == adjusted intervals + degradation trend.
    # overlap_removed is the degradation share that was subtracted out of
    # UNDERPERFORMANCE_LOCAL rows to prevent double counting.
    accounting = con.execute(
        """
        SELECT
          (SELECT sum(lost_kwh) FROM interval_ledger_raw) AS interval_raw_kwh,
          (SELECT sum(lost_kwh) FROM interval_ledger_adjusted) AS adjusted_kwh,
          (SELECT COALESCE(sum(lost_kwh), 0) FROM degradation_monthly_loss) AS degradation_kwh,
          (SELECT sum(lost_kwh) FROM interval_ledger_raw)
            - (SELECT sum(lost_kwh) FROM interval_ledger_adjusted) AS overlap_removed_kwh,
          (SELECT sum(lost_kwh) FROM ledger) AS ledger_total_kwh,
          (SELECT sum(lost_eur) FROM interval_ledger_adjusted)
            + (SELECT COALESCE(sum(lost_eur), 0) FROM degradation_monthly_loss)
            AS split_eur,
          (SELECT sum(lost_eur) FROM ledger) AS ledger_total_eur
        """
    ).fetchdf().iloc[0].to_dict()

    validation = con.execute(
        """
        SELECT
          count(*) AS top_events,
          count(*) FILTER (WHERE validated_by_ticket) AS validated_top_events,
          min(ticket_lag_days) FILTER (WHERE validated_by_ticket) AS min_lag_days,
          median(ticket_lag_days) FILTER (WHERE validated_by_ticket) AS median_lag_days,
          max(ticket_lag_days) FILTER (WHERE validated_by_ticket) AS max_lag_days
        FROM ticket_validation_events
        """
    ).fetchdf().iloc[0].to_dict()

    lead_validation = con.execute(
        """
        SELECT
          count(*) AS lead_events,
          min(ticket_lag_days) AS min_lag_days,
          median(ticket_lag_days) AS median_lag_days,
          max(ticket_lag_days) AS max_lag_days,
          sum(lost_eur) AS lead_event_lost_eur
        FROM ticket_lead_events
        """
    ).fetchdf().iloc[0].to_dict()

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": counts,
        "total_loss": {k: (None if v is None else float(v)) for k, v in total_loss.items()},
        "totals_by_bucket": totals_by_bucket.to_dict(orient="records"),
        "accounting_check": {k: (None if v is None else float(v)) for k, v in accounting.items()},
        "ticket_validation": {k: (None if v is None else float(v)) for k, v in validation.items()},
        "ticket_lead_validation": {
            k: (None if v is None else float(v)) for k, v in lead_validation.items()
        },
    }


def write_summary(metrics: dict) -> None:
    total_loss = metrics["total_loss"]
    accounting = metrics["accounting_check"]
    expected_total = accounting["adjusted_kwh"] + accounting["degradation_kwh"]
    diff_kwh = abs(accounting["ledger_total_kwh"] - expected_total)
    diff_pct = diff_kwh / expected_total * 100.0 if expected_total else 0.0

    lines = [
        "# SolarTwin Phase 4 Attribution Ledger Summary",
        "",
        f"Generated: `{metrics['generated_at']}`",
        "",
        "## Outputs",
        "",
        "- `outputs/ledger.csv`",
        "- `outputs/ledger_by_year_cause.csv`",
        "- `outputs/ledger_top20_inverter_cause.csv`",
        "- `outputs/ledger_module_type_year.csv`",
        "- `outputs/ticket_validation_events.csv`",
        "- `outputs/ticket_lead_events.csv`",
        "",
        "## Tables Created",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ]
    for table, count in metrics["counts"].items():
        lines.append(f"| `{table}` | {count:,} |")

    lines.extend(
        [
            "",
            "## Headline",
            "",
            f"- Verified production loss: `{total_loss['lost_kwh']:,.0f}` kWh.",
            f"- Verified production loss value: `EUR {total_loss['lost_eur']:,.0f}`.",
            f"- Telemetry DATA_GAP (energy unaccounted, NOT claimed as production loss): "
            f"`{(total_loss['data_gap_kwh'] or 0):,.0f}` kWh "
            f"(~`EUR {(total_loss['data_gap_eur'] or 0):,.0f}` at tariff).",
            f"- Ledger rows: `{int(total_loss['ledger_rows']):,}`.",
            f"- Ticket-validated monthly rows: `{int(total_loss['validated_rows']):,}`.",
            f"- Positive lead-time ticket examples: `{int(metrics['ticket_lead_validation']['lead_events']):,}`.",
            "",
            "## Loss by Bucket",
            "",
            "| Bucket | Lost kWh | Lost EUR | Intervals | Rows |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in metrics["totals_by_bucket"]:
        lines.append(
            f"| `{row['bucket']}` | {row['lost_kwh']:,.0f} | "
            f"{row['lost_eur']:,.0f} | {int(row['n_intervals']):,} | {int(row['ledger_rows']):,} |"
        )

    lines.extend(
        [
            "",
            "## Accounting Check",
            "",
            f"- Raw interval loss: `{accounting['interval_raw_kwh']:,.2f}` kWh.",
            f"- Adjusted intervals: `{accounting['adjusted_kwh']:,.2f}` kWh "
            f"(degradation overlap of `{accounting['overlap_removed_kwh']:,.2f}` kWh removed).",
            f"- Degradation trend loss: `{accounting['degradation_kwh']:,.2f}` kWh.",
            f"- Ledger total: `{accounting['ledger_total_kwh']:,.2f}` kWh "
            "(must equal adjusted + degradation).",
            f"- Identity difference: `{diff_pct:.4f}%`.",
            "",
            "## Classification Ladder",
            "",
            "Intervals are assigned by precedence: grid curtailment, price/operator curtailment, "
            "inverter fault, telemetry data gap, outage, local underperformance. "
            "A missing reading only counts as OUTAGE when the plant-level Janitza meter "
            "corroborates a production shortfall; otherwise it is DATA_GAP (monitoring "
            "availability issue, excluded from the verified-loss headline). "
            "UNDERPERFORMANCE_LOCAL requires the below-p10 condition to persist "
            "(>=9 of 13 intervals in a +/-30 min window) so the statistical tail of the "
            "healthy distribution is not billed as loss. "
            "Degradation is estimated monthly from the twin residual trend; its overlap with "
            "local underperformance rows is subtracted so the total is not double-counted.",
        ]
    )

    (config.OUTPUT_DIR / "ledger_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    con = duckdb.connect(str(config.DB_PATH))
    con.execute("PRAGMA threads=4")
    con.execute("PRAGMA memory_limit='10GB'")

    create_context_tables(con)
    total_kwp = float(con.execute("SELECT sum(kwp) FROM inverters").fetchone()[0])
    create_loss_intervals(con, total_kwp)
    create_ledger_tables(con)
    create_rollups_and_exports(con)
    create_ticket_validation_events(con)

    metrics = gather_metrics(con)
    (config.OUTPUT_DIR / "ledger_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    write_summary(metrics)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
