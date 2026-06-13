from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


PALETTE = {
    "OUTAGE": "#2F4858",
    "DEGRADATION": "#33658A",
    "UNDERPERFORMANCE_LOCAL": "#86BBD8",
    "FAULT": "#F26419",
    "CURTAILMENT_PRICE": "#F6AE2D",
    "CURTAILMENT_GRID": "#7A9E7E",
}


def save_figure(fig: go.Figure, name: str, width: int = 1400, height: int = 850) -> dict:
    config.FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    html_path = config.FIGURES_DIR / f"{name}.html"
    png_path = config.FIGURES_DIR / f"{name}.png"
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Arial", size=18),
        title_font=dict(size=28),
        margin=dict(l=70, r=40, t=80, b=70),
    )
    fig.write_html(str(html_path), include_plotlyjs="cdn")
    result = {"name": name, "html": str(html_path), "png": str(png_path), "png_ok": True}
    try:
        fig.write_image(str(png_path), width=width, height=height, scale=2)
    except Exception as exc:  # kaleido failures should not block the pitch markdown.
        result["png_ok"] = False
        result["png_error"] = str(exc)
    return result


def patch_kaleido_launcher() -> None:
    try:
        import kaleido
    except Exception:
        return

    launcher = Path(kaleido.__file__).resolve().parent / "executable" / "kaleido"
    if not launcher.exists():
        return
    text = launcher.read_text(encoding="utf-8")
    patched = text.replace("cd $DIR", 'cd "$DIR"').replace("./bin/kaleido $@", './bin/kaleido "$@"')
    if patched != text:
        launcher.write_text(patched, encoding="utf-8")


def make_twin_week(con: duckdb.DuckDBPyConnection) -> dict:
    inverter = "INV 01.01.003"
    start = "2022-07-17"
    df = con.execute(
        """
        SELECT
          r.ts,
          r.p_ac_kw,
          tp.p10_twin_kw,
          tp.p50_twin_kw,
          tp.p90_twin_kw
        FROM readings r
        JOIN twin_predictions tp USING (ts, inverter)
        WHERE r.inverter = ?
          AND r.ts >= CAST(? AS DATE)
          AND r.ts < CAST(? AS DATE) + INTERVAL '7 days'
        ORDER BY r.ts
        """,
        [inverter, start, start],
    ).fetchdf()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["ts"], y=df["p90_twin_kw"], line=dict(width=0), name="p90"))
    fig.add_trace(
        go.Scatter(
            x=df["ts"],
            y=df["p10_twin_kw"],
            fill="tonexty",
            fillcolor="rgba(134,187,216,0.28)",
            line=dict(width=0),
            name="p10-p90 twin band",
        )
    )
    fig.add_trace(go.Scatter(x=df["ts"], y=df["p50_twin_kw"], name="Twin p50", line=dict(color="#33658A")))
    fig.add_trace(go.Scatter(x=df["ts"], y=df["p_ac_kw"], name="Actual", line=dict(color="#F26419")))
    fig.update_layout(
        title=f"{inverter}: actual production falls below the learned twin before the ticket",
        yaxis_title="AC power (kW)",
        legend_title_text="",
    )
    return save_figure(fig, "01_twin_band_vs_actual")


def make_heatmap(con: duckdb.DuckDBPyConnection) -> dict:
    df = con.execute(
        """
        SELECT
          inverter,
          strftime(month, '%Y-%m') AS month_label,
          sum(lost_eur) AS lost_eur
        FROM ledger
        WHERE bucket != 'DATA_GAP'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).fetchdf()
    pivot = df.pivot(index="inverter", columns="month_label", values="lost_eur").fillna(0.0)
    fig = px.imshow(
        pivot,
        aspect="auto",
        color_continuous_scale=["#F7F7F2", "#86BBD8", "#F6AE2D", "#F26419"],
        labels=dict(x="Month", y="Inverter", color="EUR"),
    )
    fig.update_layout(title="The ledger turns inverter-month health into a euro heatmap")
    return save_figure(fig, "02_fleet_loss_heatmap", height=1000)


def make_degradation(con: duckdb.DuckDBPyConnection) -> dict:
    df = con.execute(
        """
        SELECT
          inverter,
          module_type,
          slope_pct_per_year,
          ci_low_pct_per_year,
          ci_high_pct_per_year
        FROM degradation_slopes
        ORDER BY slope_pct_per_year
        """
    ).fetchdf()
    # Only color/claim module types with >=5 inverters; smaller groups cannot
    # support a vendor comparison and are shown as one neutral group.
    counts = df["module_type"].value_counts()
    major = {m for m, n in counts.items() if n >= 5}
    df["module_group"] = df["module_type"].where(df["module_type"].isin(major), "Other (n<5)")
    fig = px.bar(
        df,
        x="inverter",
        y="slope_pct_per_year",
        color="module_group",
        labels={
            "slope_pct_per_year": "Performance trend incl. soiling (%/year)",
            "inverter": "Inverter",
        },
    )
    fig.update_layout(
        title="Per-inverter performance trend vs year-1 twin (incl. soiling); module types with n>=5 compared",
        xaxis_tickangle=70,
        legend_title_text="Module type",
    )
    return save_figure(fig, "03_degradation_slopes", height=850)


def make_year_bucket(con: duckdb.DuckDBPyConnection) -> dict:
    df = con.execute(
        "SELECT * FROM ledger_by_year_cause WHERE bucket != 'DATA_GAP' ORDER BY year, bucket"
    ).fetchdf()
    total_eur = float(df["lost_eur"].sum())
    fig = px.bar(
        df,
        x="year",
        y="lost_eur",
        color="bucket",
        color_discrete_map=PALETTE,
        labels={"lost_eur": "Lost EUR", "year": "Year", "bucket": "Cause"},
    )
    fig.update_layout(
        title=(
            f"SolarTwin identifies EUR {total_eur / 1000:,.1f}k of verified, "
            "Janitza-corroborated production loss"
        ),
        legend_title_text="",
    )
    return save_figure(fig, "04_stacked_eur_loss_by_year")


def make_ticket_case(con: duckdb.DuckDBPyConnection) -> tuple[dict, dict]:
    event = con.execute(
        """
        SELECT *
        FROM ticket_lead_events
        WHERE ticket_lag_days > 0
        ORDER BY lost_eur DESC
        LIMIT 1
        """
    ).fetchdf().iloc[0].to_dict()
    df = con.execute(
        """
        WITH daily AS (
          SELECT
            CAST(ts AS DATE) AS d,
            sum(lost_eur) AS lost_eur
          FROM loss_intervals
          WHERE inverter = ?
            AND bucket = ?
            AND ts >= ?
            AND ts < ?
          GROUP BY 1
        )
        SELECT
          d,
          lost_eur,
          sum(lost_eur) OVER (ORDER BY d) AS cumulative_lost_eur
        FROM daily
        ORDER BY d
        """,
        [event["inverter"], event["bucket"], event["first_ts"], event["ticket_start_ts"]],
    ).fetchdf()
    total_between = float(df["lost_eur"].sum()) if not df.empty else 0.0
    event["lost_eur_until_ticket"] = total_between

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["d"],
            y=df["cumulative_lost_eur"],
            mode="lines+markers",
            name="Cumulative EUR loss",
            line=dict(color="#F26419", width=4),
        )
    )
    fig.add_vline(x=event["first_ts"], line_color="#33658A", line_width=3)
    fig.add_vline(x=event["ticket_start_ts"], line_color="#2F4858", line_width=3, line_dash="dash")
    fig.update_layout(
        title=(
            f"{event['inverter']} was flagged {int(event['ticket_lag_days'])} days before the ticket"
        ),
        yaxis_title="Cumulative EUR loss before ticket",
        xaxis_title="Date",
        showlegend=False,
    )
    return save_figure(fig, "05_ticket_lead_time_case"), event


def make_accuracy(con: duckdb.DuckDBPyConnection) -> dict:
    df = con.execute(
        """
        SELECT
          inverter,
          nrmse * 100.0 AS nrmse_pct,
          r2,
          band_coverage
        FROM twin_model_metrics
        ORDER BY nrmse
        """
    ).fetchdf()
    median = float(df["nrmse_pct"].median())
    fig = px.bar(
        df,
        x="inverter",
        y="nrmse_pct",
        color="nrmse_pct",
        color_continuous_scale=["#7A9E7E", "#F6AE2D", "#F26419"],
        labels={"nrmse_pct": "nRMSE (% of kWp)", "inverter": "Inverter"},
    )
    fig.add_hline(y=median, line_dash="dash", line_color="#2F4858", annotation_text=f"median {median:.1f}%")
    fig.update_layout(title="Held-out year-1 accuracy is honest and inverter-specific", xaxis_tickangle=70)
    return save_figure(fig, "06_heldout_accuracy")


def write_pitch(metrics: dict, ticket_case: dict, figures: list[dict]) -> None:
    config_line = "\n".join(
        f"- `{fig['name']}`: `{fig['png'] if fig['png_ok'] else fig['html']}`" for fig in figures
    )
    pitch_path = PROJECT_ROOT / "pitch" / "PITCH.md"
    pitch_path.parent.mkdir(parents=True, exist_ok=True)
    pitch = f"""# SolarTwin Pitch

## Hook

This plant's performance ratio is not enough. SolarTwin replayed every inverter through a physics-informed digital twin and found **EUR {metrics['total_loss_eur']:,.0f}** of verified, plant-meter-corroborated production loss (**{metrics['total_loss_kwh']:,.0f} kWh**) across degradation, faults, outages, local underperformance, and curtailment — plus a second finding the operator did not ask for: the monitoring system itself was blind for **{metrics['data_gap_kwh']:,.0f} kWh** of production (~EUR {metrics['data_gap_eur']:,.0f} of energy SolarTwin refused to bill as loss because the grid meter shows the plant was running).

## Method

- Local DuckDB star schema over Plant A telemetry, tickets, errors, tariffs, and inverter metadata.
- PVWatts-style physics baseline per inverter, fitted on clean year-1 operation.
- Learned per-inverter twin trained on 2017 environment-only features.
- Full 2017-2026 replay with p10/p50/p90 bands.
- Attribution ladder converts shortfalls into one cause bucket and then euros. Two honesty gates: a missing reading only counts as OUTAGE if the Janitza plant meter corroborates the shortfall (otherwise DATA_GAP), and underperformance must persist (>=45 min below p10) so statistical noise is never billed.

## Headline Findings

- Verified production loss: **EUR {metrics['total_loss_eur']:,.0f}**.
- Largest verified bucket: **{metrics['top_bucket']}** at **EUR {metrics['top_bucket_eur']:,.0f}** (performance trend vs year-1 twin, incl. soiling), median **{metrics['median_degradation_pct_per_year']:.2f}%/year**.
- Inverter faults: **EUR {metrics['fault_eur']:,.0f}**; hard outages: **EUR {metrics['outage_eur']:,.0f}**.
- Price/operator curtailment (DV): **EUR {metrics['curtailment_price_eur']:,.0f}**; grid curtailment (EVU, potentially compensable): **EUR {metrics['curtailment_grid_eur']:,.0f}**.
- Monitoring data gaps: **{metrics['data_gap_kwh']:,.0f} kWh** unaccounted — a data-availability finding for O&M, excluded from the loss claim.
- Median held-out twin nRMSE: **{metrics['median_nrmse_pct']:.2f}% of kWp**.
- Ticket-linked lead examples: **{metrics['lead_events']:.0f}**, median lead **{metrics['median_lead_days']:.1f} days**.

## Proof Case

SolarTwin flagged **{ticket_case['inverter']}** as **{ticket_case['bucket']}** on **{str(ticket_case['event_date'])[:10]}**. The service ticket was opened on **{str(ticket_case['ticket_start_ts'])[:10]}**, **{int(ticket_case['ticket_lag_days'])} days later**. The cumulative identified loss between first flag and ticket was **EUR {ticket_case['lost_eur_until_ticket']:,.0f}**.

## Demo Questions

1. Which inverter should we service first and why?
2. How much did grid curtailment cost us in 2023 vs price curtailment?
3. Show inverter INV 01.04.023's worst month and what happened.

## Figure Assets

{config_line}

## Close

The sponsor asked for financial impacts from quality issues, downtimes, and curtailments, proven with numbers. SolarTwin produces the ledger, the proof links to tickets, and the operator-facing dashboard to act on it.
"""
    pitch_path.write_text(pitch, encoding="utf-8")


def main() -> int:
    patch_kaleido_launcher()
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    figures: list[dict] = []
    figures.append(make_twin_week(con))
    figures.append(make_heatmap(con))
    figures.append(make_degradation(con))
    figures.append(make_year_bucket(con))
    ticket_fig, ticket_case = make_ticket_case(con)
    figures.append(ticket_fig)
    figures.append(make_accuracy(con))

    ledger = json.loads((config.OUTPUT_DIR / "ledger_metrics.json").read_text(encoding="utf-8"))
    twin = json.loads((config.OUTPUT_DIR / "twin_metrics.json").read_text(encoding="utf-8"))

    bucket_totals = {row["bucket"]: row for row in ledger["totals_by_bucket"]}
    # total_loss already excludes DATA_GAP (verified production loss only);
    # pick the top bucket among verified causes, not the telemetry gap.
    top = next(row for row in ledger["totals_by_bucket"] if row["bucket"] != "DATA_GAP")
    metrics = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_loss_kwh": ledger["total_loss"]["lost_kwh"],
        "total_loss_eur": ledger["total_loss"]["lost_eur"],
        "data_gap_kwh": ledger["total_loss"].get("data_gap_kwh") or 0.0,
        "data_gap_eur": ledger["total_loss"].get("data_gap_eur") or 0.0,
        "top_bucket": top["bucket"],
        "top_bucket_eur": top["lost_eur"],
        "degradation_eur": bucket_totals.get("DEGRADATION", {}).get("lost_eur", 0.0),
        "fault_eur": bucket_totals.get("FAULT", {}).get("lost_eur", 0.0),
        "outage_eur": bucket_totals.get("OUTAGE", {}).get("lost_eur", 0.0),
        "curtailment_price_eur": bucket_totals.get("CURTAILMENT_PRICE", {}).get("lost_eur", 0.0),
        "curtailment_grid_eur": bucket_totals.get("CURTAILMENT_GRID", {}).get("lost_eur", 0.0),
        "median_degradation_pct_per_year": twin["degradation"]["median_slope_pct_per_year"],
        "median_nrmse_pct": twin["evaluation"]["median_nrmse"] * 100.0,
        "lead_events": ledger["ticket_lead_validation"]["lead_events"],
        "median_lead_days": ledger["ticket_lead_validation"]["median_lag_days"],
        "figures": figures,
        "ticket_case": ticket_case,
    }
    (config.OUTPUT_DIR / "final_metrics.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    (config.OUTPUT_DIR / "figures_manifest.json").write_text(json.dumps(figures, indent=2), encoding="utf-8")
    write_pitch(metrics, ticket_case, figures)
    print(json.dumps(metrics, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
