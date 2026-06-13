from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def numeric_expr(name: str) -> str:
    return f"TRY_CAST(REPLACE(CAST({qident(name)} AS VARCHAR), ',', '.') AS DOUBLE)"


def extract_pdf_text(path: Path) -> str:
    if not path.exists():
        return ""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def sheet_facts(path: Path, engine: str | None = None) -> dict:
    if not path.exists():
        return {"exists": False}
    try:
        xls = pd.ExcelFile(path, engine=engine)
        sheets = {}
        for sheet in xls.sheet_names:
            try:
                df = pd.read_excel(path, sheet_name=sheet, engine=engine, nrows=20)
                sheets[sheet] = {
                    "sample_rows": int(len(df)),
                    "columns": [str(c) for c in df.columns],
                }
            except Exception as exc:  # keep inspection moving
                sheets[sheet] = {"error": str(exc)}
        return {"exists": True, "sheets": sheets}
    except Exception as exc:
        return {"exists": True, "error": str(exc)}


def main() -> int:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    files = {
        "data_use_policy": config.DATA_USE_POLICY,
        "general_info_pdf": config.GENERAL_INFO_PDF,
        "main_parquet": config.MAIN_PARQUET,
        "main_xlsb": config.MAIN_XLSB,
        "main_legend_xlsb": config.MAIN_LEGEND_XLSB,
        "system_overview_xlsx": config.SYSTEM_OVERVIEW_XLSX,
        "tickets_xlsx": config.TICKETS_XLSX,
        "tariffs_xlsx": config.TARIFFS_XLSX,
        "errorcodes_parquet": config.ERRORCODES_PARQUET,
        "errorcodes_description_xlsx": config.ERRORCODES_DESCRIPTION_XLSX,
    }
    file_facts = {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2) if path.exists() else None,
        }
        for name, path in files.items()
    }

    con = duckdb.connect(":memory:")
    main_rel = f"read_parquet('{config.MAIN_PARQUET.as_posix()}')"
    error_rel = f"read_parquet('{config.ERRORCODES_PARQUET.as_posix()}')"

    parquet_file = pq.ParquetFile(config.MAIN_PARQUET)
    main_columns = parquet_file.schema_arrow.names
    main_schema = con.execute(f"DESCRIBE SELECT * FROM {main_rel}").fetchdf()
    main_rows = con.execute(f"SELECT COUNT(*) FROM {main_rel}").fetchone()[0]

    timestamp_table = pq.read_table(config.MAIN_PARQUET, columns=[config.TIMESTAMP_COL])
    timestamp_df = timestamp_table.to_pandas()
    if config.TIMESTAMP_COL in timestamp_df.columns:
        timestamp_series = timestamp_df[config.TIMESTAMP_COL]
    else:
        # The Enerparc Parquet stores timestamp as a pandas index column.
        timestamp_series = pd.Series(timestamp_df.index, name=config.TIMESTAMP_COL)
    timestamps = pd.to_datetime(timestamp_series.astype(str), format="%Y.%m.%d %H:%M", errors="coerce")
    timestamp_facts = {
        "raw_min": str(timestamp_series.min()),
        "raw_max": str(timestamp_series.max()),
        "parsed_min": str(timestamps.min()),
        "parsed_max": str(timestamps.max()),
        "n_rows": int(len(timestamp_series)),
        "n_parse_failures": int(timestamps.isna().sum()),
        "median_delta_minutes": None,
    }
    if timestamps.notna().sum() > 2:
        diffs = timestamps.sort_values().diff().dropna().dt.total_seconds() / 60
        timestamp_facts["median_delta_minutes"] = float(diffs.median())

    inverter_re = re.compile(r"^(INV \d{2}\.\d{2}\.\d{3}) / (P_AC|I_DC_SUM|U_DC)")
    inverter_tracks: dict[str, set[str]] = {}
    for col in main_columns:
        match = inverter_re.match(col)
        if match:
            inverter_tracks.setdefault(match.group(1), set()).add(match.group(2))
    inverters = sorted(inverter_tracks)

    key_columns = [
        config.DV_COL,
        config.EVU_COL,
        config.PLANT_ALTITUDE_COL,
        config.IRRADIATION_COL,
        config.AMBIENT_TEMP_COL,
        config.MODULE_TEMP_COL,
        config.JANITZA_P_AC_COL,
        config.JANITZA_COSPHI_COL,
        config.JANITZA_S_AC_COL,
    ]
    key_stats = {}
    for col in key_columns:
        if col not in main_columns:
            key_stats[col] = {"exists": False}
            continue
        expr = numeric_expr(col)
        stats = con.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              COUNT({expr}) AS n_numeric,
              MIN({expr}) AS min_value,
              AVG({expr}) AS avg_value,
              MAX({expr}) AS max_value,
              COUNT(DISTINCT {expr}) AS n_distinct
            FROM {main_rel}
            """
        ).fetchone()
        key_stats[col] = {
            "exists": True,
            "n": int(stats[0]),
            "n_numeric": int(stats[1]),
            "min": None if stats[2] is None else float(stats[2]),
            "avg": None if stats[3] is None else float(stats[3]),
            "max": None if stats[4] is None else float(stats[4]),
            "n_distinct": int(stats[5]),
        }

    sample_inverters = inverters[:3] + inverters[-3:]
    inverter_power_stats = {}
    for inverter in sample_inverters:
        col = f"{inverter} / P_AC (kW)"
        if col not in main_columns:
            continue
        expr = numeric_expr(col)
        max_v, avg_v, n_numeric = con.execute(
            f"SELECT MAX({expr}), AVG({expr}), COUNT({expr}) FROM {main_rel}"
        ).fetchone()
        inverter_power_stats[inverter] = {
            "p_ac_max_kw": None if max_v is None else float(max_v),
            "p_ac_avg_kw": None if avg_v is None else float(avg_v),
            "n_numeric": int(n_numeric),
        }

    error_pf = pq.ParquetFile(config.ERRORCODES_PARQUET)
    error_columns = error_pf.schema_arrow.names
    error_schema = con.execute(f"DESCRIBE SELECT * FROM {error_rel}").fetchdf()
    error_rows = con.execute(f"SELECT COUNT(*) FROM {error_rel}").fetchone()[0]
    error_inverters = sorted(
        {
            match.group(1)
            for col in error_columns
            if (match := re.match(r"^(INV \d{2}\.\d{2}\.\d{3}) / (Error|Operational State)", col))
        }
    )

    policy_text = config.DATA_USE_POLICY.read_text(errors="ignore").strip() if config.DATA_USE_POLICY.exists() else ""
    general_info_text = extract_pdf_text(config.GENERAL_INFO_PDF)

    excel_facts = {
        "legend": sheet_facts(config.MAIN_LEGEND_XLSB, engine="pyxlsb"),
        "system_overview": sheet_facts(config.SYSTEM_OVERVIEW_XLSX),
        "tickets": sheet_facts(config.TICKETS_XLSX),
        "tariffs": sheet_facts(config.TARIFFS_XLSX),
        "error_catalog": sheet_facts(config.ERRORCODES_DESCRIPTION_XLSX),
    }

    facts = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(config.PROJECT_ROOT),
        "data_root": str(config.DATA_ROOT),
        "files": file_facts,
        "main": {
            "rows": int(main_rows),
            "columns": len(main_columns),
            "schema": main_schema.astype(str).to_dict(orient="records"),
            "timestamp": timestamp_facts,
            "inverter_count": len(inverters),
            "inverters": inverters,
            "inverter_track_completeness": {
                inv: sorted(list(tracks)) for inv, tracks in sorted(inverter_tracks.items())
            },
            "key_stats": key_stats,
            "sample_inverter_power_stats": inverter_power_stats,
        },
        "errors": {
            "rows": int(error_rows),
            "columns": len(error_columns),
            "schema": error_schema.astype(str).to_dict(orient="records"),
            "inverter_count": len(error_inverters),
            "sample_columns": error_columns[:20],
        },
        "excel": excel_facts,
        "data_use_policy": policy_text,
        "general_info_excerpt": general_info_text[:4000],
    }

    (config.OUTPUT_DIR / "data_facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")

    lines = [
        "# SolarTwin Phase 0 Data Facts",
        "",
        f"Generated: `{facts['generated_at']}`",
        "",
        "## File Presence",
        "",
        "| File | Exists | Size MB |",
        "|---|---:|---:|",
    ]
    for name, item in file_facts.items():
        lines.append(f"| `{name}` | {item['exists']} | {item['size_mb']} |")

    lines += [
        "",
        "## Main Monitoring Parquet",
        "",
        f"- Rows: `{main_rows:,}`",
        f"- Columns: `{len(main_columns)}`",
        f"- Timestamp range: `{timestamp_facts['parsed_min']}` to `{timestamp_facts['parsed_max']}`",
        f"- Timestamp parse failures: `{timestamp_facts['n_parse_failures']}`",
        f"- Median resolution: `{timestamp_facts['median_delta_minutes']}` minutes",
        f"- Inverters detected from columns: `{len(inverters)}`",
        f"- First inverter: `{inverters[0] if inverters else 'n/a'}`",
        f"- Last inverter: `{inverters[-1] if inverters else 'n/a'}`",
        "",
        "## Key Column Ranges",
        "",
        "| Column | Numeric values | Min | Avg | Max | Distinct |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for col, stats in key_stats.items():
        if not stats.get("exists"):
            lines.append(f"| `{col}` | missing | | | | |")
        else:
            lines.append(
                f"| `{col}` | {stats['n_numeric']:,} | {stats['min']:.4g} | "
                f"{stats['avg']:.4g} | {stats['max']:.4g} | {stats['n_distinct']} |"
            )

    lines += [
        "",
        "## Sample Inverter Power Ranges",
        "",
        "| Inverter | Max P_AC kW | Avg P_AC kW | Numeric rows |",
        "|---|---:|---:|---:|",
    ]
    for inv, stats in inverter_power_stats.items():
        lines.append(
            f"| `{inv}` | {stats['p_ac_max_kw']:.4g} | {stats['p_ac_avg_kw']:.4g} | {stats['n_numeric']:,} |"
        )

    lines += [
        "",
        "## Errorcode Parquet",
        "",
        f"- Rows: `{error_rows:,}`",
        f"- Columns: `{len(error_columns)}`",
        f"- Inverters detected from error columns: `{len(error_inverters)}`",
        "",
        "## Excel/PDF Inputs",
        "",
    ]
    for name, item in excel_facts.items():
        if not item.get("exists"):
            lines.append(f"- `{name}`: missing")
        elif "error" in item:
            lines.append(f"- `{name}`: read error: `{item['error']}`")
        else:
            sheet_names = ", ".join(f"`{s}`" for s in item["sheets"].keys())
            lines.append(f"- `{name}` sheets: {sheet_names}")

    lines += [
        "",
        "## Data Use Policy",
        "",
        "```text",
        policy_text,
        "```",
        "",
        "## General Information PDF Excerpt",
        "",
        "```text",
        general_info_text[:2000],
        "```",
        "",
        "## VERIFY Items — Current Status",
        "",
        "- VERIFY-1 Timezone: partially inspected. Timestamp range and altitude track exist; precise timezone/location fit will be completed in Phase 2 if needed.",
        "- VERIFY-2 EVU/DV semantics: key ranges extracted; active-curtailment clipping check belongs to Phase 1/2 after plant capacity is joined.",
        "- VERIFY-3 Plant A location: General information PDF was extracted; location evidence must be checked from the excerpt and/or altitude fitting.",
        "- VERIFY-4 Inverter kWp/module types: System overview workbook was found; detailed normalization happens in Phase 1.",
        "- VERIFY-5 Error/Operational State encoding: Errorcode parquet and description workbook were found; sparse long-format encoding happens in Phase 1.",
        "- VERIFY-6 Units/plausibility: preliminary ranges are above; full energy sanity check runs after DuckDB ingest.",
    ]
    (config.OUTPUT_DIR / "data_facts.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"Wrote {config.OUTPUT_DIR / 'data_facts.md'}")
    print(f"Wrote {config.OUTPUT_DIR / 'data_facts.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
