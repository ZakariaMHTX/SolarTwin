from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import config  # noqa: E402
from solartwin.normalize import parse_inverter_id, safe_float  # noqa: E402


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def n_expr(name: str) -> str:
    return f"TRY_CAST(REPLACE(CAST({qident(name)} AS VARCHAR), ',', '.') AS DOUBLE)"


def ts_expr() -> str:
    return f"strptime(CAST({qident(config.TIMESTAMP_COL)} AS VARCHAR), '%Y.%m.%d %H:%M')"


def infer_inverters() -> list[str]:
    names = pq.ParquetFile(config.MAIN_PARQUET).schema_arrow.names
    found = sorted(
        {
            match.group(1)
            for col in names
            if (match := re.match(r"^(INV \d{2}\.\d{2}\.\d{3}) / P_AC", col))
        }
    )
    return found


def load_inverters() -> pd.DataFrame:
    raw = pd.read_excel(config.SYSTEM_OVERVIEW_XLSX, sheet_name="PV plant info", header=1)
    rows: list[dict] = []
    current_inv: str | None = None
    for _, row in raw.iterrows():
        desc = row.iloc[2]
        wr_type = str(row.iloc[3]) if not pd.isna(row.iloc[3]) else ""
        parsed = parse_inverter_id(desc)
        if parsed:
            current_inv = parsed
        elif isinstance(desc, str) and desc.strip().startswith("ACC"):
            current_inv = None
        if not current_inv:
            continue
        # Continuation rows often have no description but carry a second module type.
        if parsed or (pd.isna(desc) and not pd.isna(row.iloc[7])):
            pdc = safe_float(row.iloc[11])
            modules = safe_float(row.iloc[10])
            strings = safe_float(row.iloc[12])
            if pdc is None:
                continue
            rows.append(
                {
                    "inverter": current_inv,
                    "module_type": None if pd.isna(row.iloc[7]) else str(row.iloc[7]),
                    "manufacturer": None if pd.isna(row.iloc[8]) else str(row.iloc[8]),
                    "module_wp": safe_float(row.iloc[9]),
                    "modules": modules,
                    "kwp": pdc,
                    "strings": strings,
                    "modules_per_string": safe_float(row.iloc[13]),
                    "location_a": None if pd.isna(row.iloc[5]) else str(row.iloc[5]),
                    "location_b": None if pd.isna(row.iloc[6]) else str(row.iloc[6]),
                }
            )
    detail = pd.DataFrame(rows)
    if detail.empty:
        raise RuntimeError("No inverter rows parsed from System_Overview.xlsx")
    detail["combiner_group"] = detail["inverter"].str.extract(r"INV 01\.(\d{2})\.")[0]
    grouped = (
        detail.groupby("inverter", as_index=False)
        .agg(
            kwp=("kwp", "sum"),
            modules=("modules", "sum"),
            strings=("strings", "sum"),
            module_type=("module_type", lambda s: " + ".join(sorted(set(str(x) for x in s.dropna())))),
            manufacturer=("manufacturer", lambda s: " + ".join(sorted(set(str(x) for x in s.dropna())))),
            module_wp=("module_wp", "mean"),
            modules_per_string=("modules_per_string", "mean"),
            combiner_group=("combiner_group", "first"),
            location_a=("location_a", "first"),
            location_b=("location_b", "first"),
        )
    )
    return grouped.sort_values("inverter").reset_index(drop=True)


def load_tariffs() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_excel(config.TARIFFS_XLSX, sheet_name="feed-in-tarrifs", header=None)
    dates = pd.to_datetime(raw.iloc[1, 1:], errors="coerce")
    records = []
    for _, row in raw.iloc[2:].iterrows():
        inverter = parse_inverter_id(row.iloc[0])
        if not inverter:
            continue
        for date, value in zip(dates, row.iloc[1:]):
            ct = safe_float(value)
            if pd.isna(date) or ct is None:
                continue
            records.append({"inverter": inverter, "effective_date": date, "ct_per_kwh": ct})
    tariffs = pd.DataFrame(records)
    tariffs["month"] = tariffs["effective_date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        tariffs.groupby(["inverter", "month"], as_index=False)
        .agg(ct_per_kwh=("ct_per_kwh", "mean"))
        .sort_values(["inverter", "month"])
    )
    return tariffs, monthly


def load_tickets() -> pd.DataFrame:
    frames = []
    modern = pd.read_excel(config.TICKETS_XLSX, sheet_name="2020-2026")
    modern_df = pd.DataFrame(
        {
            "source_sheet": "2020-2026",
            "component": modern.get("component"),
            "start_ts": pd.to_datetime(modern.get("startdate"), errors="coerce", utc=True).dt.tz_convert(None),
            "end_ts": pd.to_datetime(modern.get("enddate"), errors="coerce", utc=True).dt.tz_convert(None),
            "category": modern.get("category"),
        }
    )
    frames.append(modern_df)

    old = pd.read_excel(config.TICKETS_XLSX, sheet_name="2019-2020")
    start = pd.to_datetime(
        old.get("Start Date").astype(str) + " " + old.get("Uhrzeit Beginn").astype(str),
        errors="coerce",
    )
    end = pd.to_datetime(
        old.get("Datum Ende").astype(str) + " " + old.get("Uhrzeit Ende").astype(str),
        errors="coerce",
    )
    old_df = pd.DataFrame(
        {
            "source_sheet": "2019-2020",
            "component": old.get("Komponente"),
            "start_ts": start,
            "end_ts": end,
            "category": old.get("Störungsart/ Beanstandung"),
        }
    )
    frames.append(old_df)
    tickets = pd.concat(frames, ignore_index=True)
    tickets["inverter"] = tickets["component"].map(parse_inverter_id)
    tickets["text"] = (
        tickets["component"].fillna("").astype(str)
        + " | "
        + tickets["category"].fillna("").astype(str)
    )
    return tickets


def severity_for(code: int | float | None, description: str | None) -> str:
    if code in (None, 0) or pd.isna(code):
        return "none"
    text = (description or "").lower()
    if any(word in text for word in ["warn", "temperatur", "spannung", "strom"]):
        return "warning"
    return "fault"


def load_error_catalog() -> pd.DataFrame:
    raw = pd.read_excel(config.ERRORCODES_DESCRIPTION_XLSX, sheet_name="Refu Fehlercode")
    df = pd.DataFrame(
        {
            "component_type": raw.get("Unnamed: 0"),
            "hex": raw.get("Hex"),
            "code": pd.to_numeric(raw.get("Dezimal"), errors="coerce"),
            "description": raw.get("Code"),
        }
    )
    df = df.dropna(subset=["code"]).copy()
    df["code"] = df["code"].astype(int)
    df["severity_class"] = [severity_for(c, d) for c, d in zip(df["code"], df["description"])]
    return df.drop_duplicates(subset=["code"]).sort_values("code")


def register_df(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> None:
    con.register("_tmp_df", df)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _tmp_df")
    con.unregister("_tmp_df")


def main() -> int:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()

    inverters = infer_inverters()
    con = duckdb.connect(str(config.DB_PATH))
    con.execute("PRAGMA threads=4")
    con.execute("PRAGMA memory_limit='10GB'")

    main_rel = f"read_parquet('{config.MAIN_PARQUET.as_posix()}')"
    err_rel = f"read_parquet('{config.ERRORCODES_PARQUET.as_posix()}')"

    print("Loading metadata workbooks...")
    register_df(con, "inverters", load_inverters())
    tariff_weekly, tariff_monthly = load_tariffs()
    register_df(con, "tariffs_weekly", tariff_weekly)
    register_df(con, "tariffs_monthly", tariff_monthly)
    register_df(con, "tickets", load_tickets())
    register_df(con, "error_catalog", load_error_catalog())

    print("Creating plant table...")
    con.execute(
        f"""
        CREATE TABLE plant AS
        SELECT
          {ts_expr()} AS ts,
          {n_expr(config.IRRADIATION_COL)} AS irradiation_w_m2,
          {n_expr(config.PLANT_ALTITUDE_COL)} AS altitude_deg,
          {n_expr(config.AMBIENT_TEMP_COL)} AS ambient_temp_c,
          {n_expr(config.MODULE_TEMP_COL)} AS module_temp_c,
          {n_expr(config.DV_COL)} AS dv_pct,
          {n_expr(config.EVU_COL)} AS evu_pct,
          {n_expr(config.JANITZA_P_AC_COL)} AS janitza_p_ac_kw,
          {n_expr(config.JANITZA_COSPHI_COL)} AS cosphi,
          {n_expr(config.JANITZA_S_AC_COL)} AS s_ac_kva,
          ({n_expr(config.PLANT_ALTITUDE_COL)} > {config.DAY_ALTITUDE_DEG}) AS is_day,
          ({n_expr(config.DV_COL)} < {config.CURTAILMENT_FREE_PCT}
            OR {n_expr(config.EVU_COL)} < {config.CURTAILMENT_FREE_PCT}) AS is_curtailed
        FROM {main_rel}
        """
    )

    print("Creating long readings table; this is the largest Phase 1 step...")
    selects = []
    for inverter in inverters:
        selects.append(
            f"""
            SELECT
              {ts_expr()} AS ts,
              '{inverter}' AS inverter,
              {n_expr(f'{inverter} / P_AC (kW)')} AS p_ac_kw,
              {n_expr(f'{inverter} / I_DC_SUM (A)')} AS i_dc_a,
              {n_expr(f'{inverter} / U_DC (V)')} AS u_dc_v
            FROM {main_rel}
            """
        )
    con.execute("CREATE TABLE readings AS " + "\nUNION ALL\n".join(selects))

    print("Creating sparse error_events table...")
    err_selects = []
    for inverter in inverters:
        err_selects.append(
            f"""
            SELECT
              {ts_expr()} AS ts,
              '{inverter}' AS inverter,
              TRY_CAST({qident(f'{inverter} / Error')} AS BIGINT) AS error_code,
              TRY_CAST({qident(f'{inverter} / Operational State')} AS BIGINT) AS operational_state
            FROM {err_rel}
            WHERE TRY_CAST({qident(f'{inverter} / Error')} AS BIGINT) IS NOT NULL
              AND TRY_CAST({qident(f'{inverter} / Error')} AS BIGINT) != 0
            """
        )
    con.execute("CREATE TABLE error_events AS " + "\nUNION ALL\n".join(err_selects))

    print("Creating helper views and aggregates...")
    con.execute(
        """
        CREATE VIEW readings_with_context AS
        SELECT
          r.*,
          i.kwp,
          i.module_type,
          i.manufacturer,
          i.combiner_group,
          p.irradiation_w_m2,
          p.altitude_deg,
          p.ambient_temp_c,
          p.module_temp_c,
          p.dv_pct,
          p.evu_pct,
          p.is_day,
          p.is_curtailed,
          (r.p_ac_kw / NULLIF(i.kwp, 0)) AS p_ac_per_kwp
        FROM readings r
        LEFT JOIN inverters i USING (inverter)
        LEFT JOIN plant p USING (ts)
        """
    )
    con.execute(
        """
        CREATE TABLE fleet_5min AS
        SELECT
          r.ts,
          median(r.p_ac_kw / NULLIF(i.kwp, 0)) AS fleet_median_kw_per_kwp,
          avg(r.p_ac_kw / NULLIF(i.kwp, 0)) AS fleet_avg_kw_per_kwp,
          count(*) FILTER (WHERE r.p_ac_kw IS NOT NULL) AS n_reporting
        FROM readings r
        JOIN inverters i USING (inverter)
        JOIN plant p USING (ts)
        WHERE p.is_day
        GROUP BY r.ts
        """
    )
    con.execute(
        """
        CREATE TABLE readings_monthly AS
        SELECT
          date_trunc('month', r.ts) AS month,
          r.inverter,
          sum(GREATEST(r.p_ac_kw, 0) * (5.0/60.0)) AS energy_kwh,
          avg(r.p_ac_kw / NULLIF(i.kwp, 0)) FILTER (WHERE p.is_day) AS avg_day_kw_per_kwp,
          count(*) AS n_intervals,
          count(*) FILTER (WHERE p.is_day) AS n_day_intervals
        FROM readings r
        JOIN inverters i USING (inverter)
        JOIN plant p USING (ts)
        GROUP BY 1, 2
        """
    )

    print("Running sanity checks...")
    counts = {
        "plant": con.execute("SELECT COUNT(*) FROM plant").fetchone()[0],
        "readings": con.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
        "inverters": con.execute("SELECT COUNT(*) FROM inverters").fetchone()[0],
        "error_events": con.execute("SELECT COUNT(*) FROM error_events").fetchone()[0],
        "tickets": con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
        "tariffs_monthly": con.execute("SELECT COUNT(*) FROM tariffs_monthly").fetchone()[0],
    }
    capacity_kwp = con.execute("SELECT SUM(kwp) FROM inverters").fetchone()[0]
    energy_check = con.execute(
        """
        WITH inv AS (
          SELECT ts, SUM(p_ac_kw) AS inverter_p_ac_kw
          FROM readings
          WHERE ts >= TIMESTAMP '2018-06-01' AND ts < TIMESTAMP '2018-06-08'
          GROUP BY ts
        )
        SELECT
          avg(inverter_p_ac_kw) AS avg_inverter_kw,
          avg(-p.janitza_p_ac_kw) AS avg_janitza_feed_in_kw,
          avg(abs(inverter_p_ac_kw + p.janitza_p_ac_kw)) AS avg_abs_gap_kw,
          avg(abs(inverter_p_ac_kw + p.janitza_p_ac_kw) / NULLIF(inverter_p_ac_kw, 0)) AS avg_rel_gap
        FROM inv
        JOIN plant p USING (ts)
        WHERE p.is_day AND p.janitza_p_ac_kw IS NOT NULL AND inverter_p_ac_kw > 10
        """
    ).fetchdf().iloc[0].to_dict()
    p_ac_exceed = con.execute(
        """
        SELECT COUNT(*) FROM readings r
        JOIN inverters i USING (inverter)
        WHERE r.p_ac_kw > i.kwp * 1.1
        """
    ).fetchone()[0]

    metrics = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {k: int(v) for k, v in counts.items()},
        "capacity_kwp": float(capacity_kwp),
        "energy_check_sample_week": {k: (None if pd.isna(v) else float(v)) for k, v in energy_check.items()},
        "energy_check_window": "2018-06-01 to 2018-06-08",
        "p_ac_exceeds_1p1_kwp_rows": int(p_ac_exceed),
        "database_path": str(config.DB_PATH),
    }
    (config.OUTPUT_DIR / "ingest_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    lines = [
        "# SolarTwin Phase 1 Ingest Summary",
        "",
        f"Generated: `{metrics['generated_at']}`",
        "",
        "## DuckDB Tables",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ]
    for name, count in counts.items():
        lines.append(f"| `{name}` | {count:,} |")
    lines += [
        "",
        "## Plant Capacity",
        "",
        f"- Parsed inverter capacity: `{capacity_kwp:.2f}` kWp",
        f"- Inverters in metadata: `{counts['inverters']}`",
        "",
        "## Sanity Checks",
        "",
        "Sample window for Janitza comparison: `2018-06-01` to `2018-06-08`.",
        f"- Rows where `P_AC > 1.1 × kWp`: `{p_ac_exceed:,}`",
        f"- Sample week avg inverter power: `{energy_check['avg_inverter_kw']:.2f}` kW",
        f"- Sample week avg Janitza feed-in after sign correction: `{energy_check['avg_janitza_feed_in_kw']:.2f}` kW",
        f"- Sample week avg relative gap: `{energy_check['avg_rel_gap']:.2%}`",
        "",
        "## Notes",
        "",
        "- `readings` is the largest table: one row per timestamp and inverter.",
        "- `error_events` is sparse and only stores non-zero inverter error codes.",
        "- `readings_monthly` and `fleet_5min` are pre-aggregated helpers for later modeling/dashboard work.",
    ]
    (config.OUTPUT_DIR / "ingest_summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
