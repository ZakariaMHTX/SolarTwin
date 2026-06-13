from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import config  # noqa: E402
from solartwin.agent import answer_question, answer_with_templates  # noqa: E402


st.set_page_config(
    page_title="SolarTwin — Plant A Reliability Copilot",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------

INK = "#10212F"
MUTED = "#5E6E7E"
LINE = "#E4E9EF"
GRID = "#ECF0F4"
ORANGE = "#F26419"
NAVY = "#33658A"

PALETTE = {
    "DATA_GAP": "#C2C8CE",
    "OUTAGE": "#2F4858",
    "DEGRADATION": "#33658A",
    "UNDERPERFORMANCE_LOCAL": "#86BBD8",
    "FAULT": "#F26419",
    "CURTAILMENT_PRICE": "#F6AE2D",
    "CURTAILMENT_GRID": "#7A9E7E",
}

BUCKET_LABELS = {
    "DEGRADATION": "Degradation",
    "FAULT": "Fault",
    "OUTAGE": "Outage",
    "UNDERPERFORMANCE_LOCAL": "Underperformance",
    "CURTAILMENT_PRICE": "Curtailment — price (DV)",
    "CURTAILMENT_GRID": "Curtailment — grid (EVU)",
    "DATA_GAP": "Telemetry gap",
}
LABEL_PALETTE = {BUCKET_LABELS[k]: v for k, v in PALETTE.items()}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"], [data-testid="stAppViewContainer"] * {
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
}
[data-testid="stAppViewContainer"] { background: #F4F6F8; }
[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer,
[data-testid="stToolbar"],
[data-testid="stToolbarActions"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stAppDeployButton"],
[data-testid="manage-app-button"],
[data-testid="deploy-button"],
[data-testid="stBaseButton-header"],
button[title="Deploy"],
button[aria-label="Deploy"] {
    display: none !important;
    visibility: hidden !important;
}
.block-container { padding-top: 1.0rem; padding-bottom: 3rem; max-width: 1380px; }

/* ---------- branded header ---------- */
.st-hero {
    display: flex; align-items: center; gap: 16px;
    padding: 18px 22px; margin-bottom: 14px;
    background: linear-gradient(135deg, #10212F 0%, #1C3A52 70%, #27506F 100%);
    border-radius: 16px; color: #FFFFFF;
}
.st-hero .mark {
    width: 46px; height: 46px; border-radius: 12px; flex: 0 0 46px;
    background: linear-gradient(135deg, #F26419, #F6AE2D);
    display: flex; align-items: center; justify-content: center;
    font-size: 24px;
}
.st-hero h1 { font-size: 1.25rem; font-weight: 800; margin: 0; letter-spacing: -0.01em; color: #fff; }
.st-hero p  { margin: 2px 0 0; font-size: 0.82rem; color: #B9C6D2; }
.st-hero .badges { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
.st-hero .badge {
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.02em;
    padding: 5px 11px; border-radius: 999px;
    background: rgba(255,255,255,0.10); border: 1px solid rgba(255,255,255,0.18);
    color: #E7EEF4; white-space: nowrap;
}

/* ---------- KPI cards ---------- */
.kpi-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin: 4px 0 6px; }
@media (max-width: 1100px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }
.kpi {
    position: relative; background: #FFFFFF; border: 1px solid #E4E9EF;
    border-radius: 14px; padding: 14px 16px 12px;
    box-shadow: 0 1px 2px rgba(16,33,47,0.04);
    overflow: hidden;
}
.kpi::before {
    content: ""; position: absolute; inset: 0 auto auto 0; width: 100%; height: 3px;
    background: var(--accent, #33658A);
}
.kpi .lbl { font-size: 0.68rem; font-weight: 700; letter-spacing: 0.09em;
            text-transform: uppercase; color: #5E6E7E; }
.kpi .val { font-size: 1.5rem; font-weight: 800; color: #10212F; margin-top: 3px;
            letter-spacing: -0.02em; line-height: 1.15; }
.kpi .sub { font-size: 0.74rem; color: #5E6E7E; margin-top: 3px; }

/* ---------- section headings ---------- */
.sec { margin: 0 0 8px; }
.sec .eyebrow { font-size: 0.66rem; font-weight: 700; letter-spacing: 0.12em;
                text-transform: uppercase; color: #F26419; }
.sec h3 { font-size: 1.02rem; font-weight: 700; color: #10212F; margin: 1px 0 0; }
.sec p  { font-size: 0.8rem; color: #5E6E7E; margin: 2px 0 0; }

/* ---------- panel cards (native bordered containers) ----------
   Each panel = one st.container(border=True). Streamlit manages its border,
   padding and the vertical gap BETWEEN panels, so panels can never overlap.
   We only soften the look (rounder corners, subtle shadow). */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
    border-radius: 14px;
}
[data-testid="stExpander"] details { border-radius: 12px; }

/* ---------- tabs as segmented control ---------- */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px; background: #E9EDF2; padding: 4px; border-radius: 12px;
    width: fit-content;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 9px; padding: 7px 18px; font-weight: 600; font-size: 0.86rem;
    color: #5E6E7E; background: transparent;
}
.stTabs [aria-selected="true"] {
    background: #FFFFFF !important; color: #10212F !important;
    box-shadow: 0 1px 3px rgba(16,33,47,0.10);
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { background: #10212F; }
[data-testid="stSidebar"] * { color: #C9D5DF; }
[data-testid="stSidebar"] .side-brand { display:flex; align-items:center; gap:10px; margin: 6px 0 14px; }
[data-testid="stSidebar"] .side-brand .mark {
    width: 34px; height: 34px; border-radius: 9px;
    background: linear-gradient(135deg, #F26419, #F6AE2D);
    display:flex; align-items:center; justify-content:center; font-size: 17px;
}
[data-testid="stSidebar"] .side-brand b { color: #FFFFFF; font-size: 1.0rem; letter-spacing: -0.01em; }
[data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.12); }
.side-fact { display:flex; justify-content:space-between; font-size: 0.8rem;
             padding: 5px 0; border-bottom: 1px dashed rgba(255,255,255,0.10); }
.side-fact b { color: #FFFFFF; font-weight: 600; }
.side-note { font-size: 0.73rem; color: #8FA2B2; line-height: 1.5; }
.side-foot { font-size: 0.72rem; color: #8FA2B2; margin-top: 14px; }
.side-foot b { color: #F6AE2D; }

/* chips */
.chip-row { display:flex; gap:8px; flex-wrap: wrap; margin: 2px 0 10px; }
.chip { font-size: 0.74rem; font-weight: 600; padding: 5px 12px; border-radius: 999px;
        background:#FFFFFF; border:1px solid #E4E9EF; color:#10212F; }
.chip b { color:#F26419; }

div[data-testid="stForm"] { background:#FFFFFF; border:1px solid #E4E9EF; border-radius:14px; padding: 14px; }

/* ---------- clickability safety ----------
   Decorative HTML injected via st.markdown (hero, KPI cards, section
   headers, chips) must NEVER intercept clicks meant for the real widgets
   beneath/around them. Make the decorative layers transparent to the
   pointer, and lift the interactive widgets to the top of the stack. */
.st-hero, .kpi-row, .kpi, .sec, .chip-row, .side-fact, .side-note, .side-foot {
    pointer-events: none;
}
[data-testid="stButton"], [data-testid="stFormSubmitButton"],
[data-testid="stDownloadButton"], [data-testid="stSelectbox"],
[data-testid="stMultiSelect"], [data-testid="stTextInput"],
[data-testid="stExpander"], .stTabs [data-baseweb="tab-list"] {
    position: relative; z-index: 3;
}
.stButton > button, .stFormSubmitButton > button { cursor: pointer; }
</style>
"""

# ---------------------------------------------------------------------------
# Data access (unchanged logic)
# ---------------------------------------------------------------------------


def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(config.DB_PATH), read_only=True)


@st.cache_data(show_spinner=False)
def load_plant_facts() -> dict:
    with con() as db:
        row = db.execute(
            """
            SELECT
              (SELECT count(*) FROM inverters) AS n_inverters,
              (SELECT round(sum(kwp), 1) FROM inverters) AS kwp,
              (SELECT strftime(min(month), '%Y-%m') FROM ledger) AS first_month,
              (SELECT strftime(max(month), '%Y-%m') FROM ledger) AS last_month,
              (SELECT count(*) FROM ledger) AS ledger_rows
            """
        ).fetchdf().iloc[0].to_dict()
    return row


@st.cache_data(show_spinner=False)
def load_kpis() -> dict:
    with con() as db:
        total = db.execute(
            """
            SELECT
              sum(lost_eur) FILTER (WHERE bucket != 'DATA_GAP') AS lost_eur,
              sum(lost_kwh) FILTER (WHERE bucket != 'DATA_GAP') AS lost_kwh,
              COALESCE(sum(lost_kwh) FILTER (WHERE bucket = 'DATA_GAP'), 0) AS data_gap_kwh,
              count(*) AS ledger_rows,
              count(*) FILTER (WHERE validated_by_ticket) AS validated_rows
            FROM ledger
            """
        ).fetchdf().iloc[0].to_dict()
        top_inv = db.execute(
            """
            SELECT inverter, sum(lost_eur) AS lost_eur
            FROM ledger
            WHERE bucket IN ('FAULT', 'OUTAGE', 'UNDERPERFORMANCE_LOCAL')
            GROUP BY inverter ORDER BY lost_eur DESC LIMIT 1
            """
        ).fetchdf().iloc[0].to_dict()
        top_bucket = db.execute(
            """
            SELECT bucket, sum(lost_eur) AS lost_eur
            FROM ledger
            WHERE bucket != 'DATA_GAP'
            GROUP BY bucket ORDER BY lost_eur DESC LIMIT 1
            """
        ).fetchdf().iloc[0].to_dict()
    return {"total": total, "top_inv": top_inv, "top_bucket": top_bucket}


@st.cache_data(show_spinner=False)
def load_year_cause() -> pd.DataFrame:
    with con() as db:
        return db.execute("SELECT * FROM ledger_by_year_cause").fetchdf()


@st.cache_data(show_spinner=False)
def load_heatmap() -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT inverter, strftime(month, '%Y-%m') AS month_label, sum(lost_eur) AS lost_eur
            FROM ledger
            WHERE bucket != 'DATA_GAP'
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_inverter_options() -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT inverter, sum(lost_eur) FILTER (WHERE bucket != 'DATA_GAP') AS lost_eur
            FROM ledger GROUP BY inverter ORDER BY lost_eur DESC
            """
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_inverter_months(inverter: str) -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT month, bucket, sum(lost_eur) AS lost_eur, sum(lost_kwh) AS lost_kwh,
                   bool_or(validated_by_ticket) AS validated_by_ticket
            FROM ledger WHERE inverter = ?
            GROUP BY 1, 2 ORDER BY lost_eur DESC
            """,
            [inverter],
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_inverter_timeseries(inverter: str, month: str) -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT r.ts, r.p_ac_kw, tp.p10_twin_kw, tp.p50_twin_kw, tp.p90_twin_kw,
                   li.bucket, li.lost_eur
            FROM readings r
            JOIN twin_predictions tp USING (ts, inverter)
            LEFT JOIN loss_intervals li USING (ts, inverter)
            WHERE r.inverter = ?
              AND r.ts >= CAST(? AS DATE)
              AND r.ts < CAST(? AS DATE) + INTERVAL '1 month'
            ORDER BY r.ts
            """,
            [inverter, month, month],
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_inverter_tickets(inverter: str) -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT start_ts, end_ts, category, text
            FROM tickets WHERE inverter = ? AND start_ts IS NOT NULL
            ORDER BY start_ts
            """,
            [inverter],
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_ledger_filtered(years: list[int], buckets: list[str]) -> pd.DataFrame:
    where, params = [], []
    if years:
        where.append("year IN (" + ",".join(["?"] * len(years)) + ")")
        params.extend(years)
    if buckets:
        where.append("bucket IN (" + ",".join(["?"] * len(buckets)) + ")")
        params.extend(buckets)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with con() as db:
        return db.execute(
            f"""
            SELECT inverter, year, month, bucket, error_code, error_description,
                   round(lost_kwh, 1) AS lost_kwh, round(lost_eur, 1) AS lost_eur,
                   n_intervals, validated_by_ticket
            FROM ledger {clause}
            ORDER BY lost_eur DESC LIMIT 1000
            """,
            params,
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_ledger_filter_summary(years: list[int], buckets: list[str]) -> dict:
    where, params = [], []
    if years:
        where.append("year IN (" + ",".join(["?"] * len(years)) + ")")
        params.extend(years)
    if buckets:
        where.append("bucket IN (" + ",".join(["?"] * len(buckets)) + ")")
        params.extend(buckets)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with con() as db:
        totals = db.execute(
            f"""
            SELECT
              COALESCE(sum(lost_eur), 0) AS lost_eur,
              COALESCE(sum(lost_kwh), 0) AS lost_kwh,
              count(*) AS rows,
              count(*) FILTER (WHERE validated_by_ticket) AS ticket_rows
            FROM ledger
            {clause}
            """,
            params,
        ).fetchdf().iloc[0].to_dict()
        by_bucket = db.execute(
            f"""
            SELECT bucket, sum(lost_eur) AS lost_eur
            FROM ledger
            {clause}
            GROUP BY bucket
            ORDER BY lost_eur
            """,
            params,
        ).fetchdf()
    return {"totals": totals, "by_bucket": by_bucket}


@st.cache_data(show_spinner=False)
def load_error_ranking() -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT
              COALESCE(error_description, 'code ' || CAST(error_code_base AS VARCHAR)) AS error,
              count(*) AS months_affected,
              round(sum(lost_eur)) AS lost_eur
            FROM ledger
            WHERE bucket = 'FAULT'
            GROUP BY 1 ORDER BY lost_eur DESC LIMIT 8
            """
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_degradation() -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT inverter, module_type, slope_pct_per_year,
                   ci_low_pct_per_year, ci_high_pct_per_year, n_months
            FROM degradation_slopes ORDER BY slope_pct_per_year
            """
        ).fetchdf()


@st.cache_data(show_spinner=False)
def load_lead_events() -> pd.DataFrame:
    with con() as db:
        return db.execute(
            """
            SELECT event_date, inverter, bucket, round(lost_eur, 1) AS lost_eur,
                   ticket_start_ts, ticket_lag_days, ticket_category
            FROM ticket_lead_events ORDER BY lost_eur DESC LIMIT 50
            """
        ).fetchdf()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def money(value: float) -> str:
    return f"€ {value:,.0f}"


def kwh(value: float) -> str:
    return f"{value:,.0f} kWh"


def style_fig(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=8, r=8, t=12, b=8),
        font=dict(family="Inter, sans-serif", size=12.5, color=INK),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11.5)),
        hoverlabel=dict(bgcolor=INK, font_color="#FFFFFF", font_family="Inter"),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=LINE)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=LINE)
    return fig


def section(eyebrow: str, title: str, caption: str | None = None) -> None:
    cap = f"<p>{caption}</p>" if caption else ""
    st.markdown(
        f'<div class="sec"><div class="eyebrow">{eyebrow}</div><h3>{title}</h3>{cap}</div>',
        unsafe_allow_html=True,
    )


@contextmanager
def panel(eyebrow: str, title: str, caption: str | None = None):
    """One self-contained card: a section header + its content inside a native
    bordered container. Streamlit owns the spacing between panels, so adjacent
    panels (across columns or rows) can never visually overlap."""
    with st.container(border=True):
        section(eyebrow, title, caption)
        yield


def kpi_row(cards: list[tuple[str, str, str, str]]) -> None:
    html = "".join(
        f'<div class="kpi" style="--accent:{accent}">'
        f'<div class="lbl">{label}</div><div class="val">{value}</div>'
        f'<div class="sub">{sub}</div></div>'
        for label, value, sub, accent in cards
    )
    st.markdown(f'<div class="kpi-row">{html}</div>', unsafe_allow_html=True)


def pretty_buckets(df: pd.DataFrame, col: str = "bucket") -> pd.DataFrame:
    out = df.copy()
    out[col] = out[col].map(BUCKET_LABELS).fillna(out[col])
    return out


def render_header(facts: dict) -> None:
    st.markdown(
        f"""
        <div class="st-hero">
          <div class="mark">☀️</div>
          <div>
            <h1>SolarTwin</h1>
            <p>Digital-twin reliability copilot &middot; Enerparc Plant A</p>
          </div>
          <div class="badges">
            <span class="badge">{facts['n_inverters']:.0f} inverters</span>
            <span class="badge">{facts['kwp']:,.0f} kWp</span>
            <span class="badge">{facts['first_month']} → {facts['last_month']}</span>
            <span class="badge">5-min telemetry</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar(facts: dict, kpis: dict) -> None:
    with st.sidebar:
        st.markdown(
            '<div class="side-brand"><div class="mark">☀️</div><b>SolarTwin</b></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="side-note">A per-inverter digital twin trained on the first year of '
            "healthy operation, replayed over nine years, converting every missing kWh into "
            "an attributed, euro-denominated loss ledger.</p>",
            unsafe_allow_html=True,
        )
        st.markdown("---")
        for label, val in [
            ("Verified loss", money(kpis["total"]["lost_eur"])),
            ("Lost energy", kwh(kpis["total"]["lost_kwh"])),
            ("Telemetry gaps", kwh(kpis["total"]["data_gap_kwh"])),
            ("Inverters", f"{facts['n_inverters']:.0f}"),
            ("Capacity", f"{facts['kwp']:,.0f} kWp"),
            ("Coverage", f"{facts['first_month']} → {facts['last_month']}"),
        ]:
            st.markdown(
                f'<div class="side-fact"><span>{label}</span><b>{val}</b></div>',
                unsafe_allow_html=True,
            )
        st.markdown("---")
        st.markdown(
            '<p class="side-note"><b style="color:#fff">Honesty gates.</b> A missing reading '
            "only counts as outage when the plant meter confirms the shortfall; "
            "underperformance must persist ≥45 min below the twin's p10 band. "
            "Statistical noise and monitoring blackouts are never billed as loss.</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="side-foot">Energy × AI Hackathon · Munich 2026</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def plant_health_tab(kpis: dict) -> None:
    t = kpis["total"]
    kpi_row(
        [
            ("Verified loss", money(t["lost_eur"]), "plant-meter corroborated · 9.4 years", ORANGE),
            ("Lost energy", kwh(t["lost_kwh"]), "vs year-1 digital twin", NAVY),
            (
                "Top cause",
                BUCKET_LABELS.get(kpis["top_bucket"]["bucket"], kpis["top_bucket"]["bucket"]),
                money(kpis["top_bucket"]["lost_eur"]),
                PALETTE.get(kpis["top_bucket"]["bucket"], NAVY),
            ),
            (
                "Top actionable inverter",
                str(kpis["top_inv"]["inverter"]).replace("INV ", ""),
                money(kpis["top_inv"]["lost_eur"]) + " · fault / outage / underperf.",
                "#2F4858",
            ),
            (
                "Telemetry gaps",
                kwh(t["data_gap_kwh"]),
                "monitoring blind — not billed as loss",
                "#C2C8CE",
            ),
        ]
    )

    year_cause = pretty_buckets(load_year_cause())
    df = year_cause[year_cause["bucket"] != BUCKET_LABELS["DATA_GAP"]]
    left, right = st.columns([1.9, 1.0], gap="medium")
    with left:
        with panel("Loss over time", "Verified loss per year, by cause"):
            fig = px.bar(
                df, x="year", y="lost_eur", color="bucket",
                color_discrete_map=LABEL_PALETTE,
                labels={"lost_eur": "Lost €", "year": "", "bucket": ""},
            )
            fig.update_traces(hovertemplate="%{fullData.name}<br>%{x}: € %{y:,.0f}<extra></extra>")
            st.plotly_chart(style_fig(fig, 340), use_container_width=True, config={"displayModeBar": False})
    with right:
        with panel("Cause share", "Share of total verified loss"):
            share = (
                df.groupby("bucket", as_index=False)["lost_eur"].sum().sort_values("lost_eur", ascending=False)
            )
            donut = px.pie(
                share, names="bucket", values="lost_eur", hole=0.62,
                color="bucket", color_discrete_map=LABEL_PALETTE,
            )
            donut.update_traces(
                textinfo="none",
                hovertemplate="%{label}: € %{value:,.0f} (%{percent})<extra></extra>",
            )
            donut.update_layout(
                annotations=[
                    dict(
                        text=f"<b>{money(share['lost_eur'].sum())}</b><br><span style='font-size:11px;color:#5E6E7E'>verified</span>",
                        showarrow=False, font=dict(size=15, color=INK),
                    )
                ],
                showlegend=True,
            )
            st.plotly_chart(style_fig(donut, 340), use_container_width=True, config={"displayModeBar": False})

    with panel(
        "Fleet health map",
        "Verified € loss per inverter per month",
        "Vertical stripes = plant-wide events · horizontal streaks = a struggling inverter. Open the Deep-Dive tab for any inverter.",
    ):
        heat = load_heatmap()
        pivot = heat.pivot(index="inverter", columns="month_label", values="lost_eur").fillna(0.0)
        pivot = pivot.sort_index()
        pivot.index = [i.replace("INV ", "") for i in pivot.index]
        months_axis = list(pivot.columns)
        # 114 monthly columns overlap if every label is drawn — show one tick per
        # year (each January) and label it with the year only.
        year_ticks = [(m, m[:4]) for m in months_axis if m.endswith("-01")]
        heat_fig = go.Figure(
            data=go.Heatmap(
                z=pivot.values,
                x=list(range(len(months_axis))),
                y=list(pivot.index),
                customdata=[months_axis for _ in range(len(pivot.index))],
                colorscale=[
                    [0.0, "#FFFFFF"],
                    [0.25, "#C7D8E8"],
                    [0.70, "#33658A"],
                    [1.0, "#F26419"],
                ],
                colorbar=dict(title="€", thickness=12),
                xgap=0.5,
                ygap=0.5,
                hovertemplate="INV %{y} · %{customdata}<br>€ %{z:,.0f}<extra></extra>",
            )
        )
        heat_fig.update_xaxes(
            tickmode="array",
            tickvals=[months_axis.index(m) for m, _ in year_ticks],
            ticktext=[t for _, t in year_ticks],
            tickangle=0,
            side="top",
            tickfont=dict(size=12, color=INK),
        )
        heat_fig.update_yaxes(tickfont=dict(size=9.5))
        heat_fig.update_layout(coloraxis_colorbar=dict(title="€", thickness=12))
        st.plotly_chart(
            style_fig(heat_fig, 720),
            use_container_width=True,
            config={"displayModeBar": False},
        )


def inverter_tab() -> None:
    options = load_inverter_options()
    loss_map = dict(zip(options["inverter"], options["lost_eur"]))
    deg = load_degradation()

    c1, c2 = st.columns([1.1, 1.0], gap="small")
    with c1:
        inverter = st.selectbox(
            "Inverter (sorted by verified loss)",
            options["inverter"].tolist(),
            format_func=lambda inv: f"{inv}   ·   {money(loss_map.get(inv, 0) or 0)}",
        )
    months = load_inverter_months(inverter)
    month_totals = (
        months[months["bucket"] != "DATA_GAP"]
        .groupby("month", as_index=False)["lost_eur"].sum()
        .sort_values("lost_eur", ascending=False)
    )
    with c2:
        month_options = month_totals["month"].astype(str).str[:10].tolist()
        month_loss = dict(zip(month_options, month_totals["lost_eur"]))
        if not month_options:
            # Inverter has only telemetry-gap rows and no verified loss — fall
            # back to its most recent month so the page still renders.
            month_options = sorted(months["month"].astype(str).str[:10].unique(), reverse=True)
            month_loss = {}
        month = st.selectbox(
            "Month (sorted by loss)",
            month_options,
            format_func=lambda m: f"{m[:7]}   ·   {money(month_loss.get(m, 0))}",
        ) if month_options else None
    if not month:
        st.info("No monthly data available for this inverter.")
        return

    inv_deg = deg[deg["inverter"] == inverter]
    slope = float(inv_deg["slope_pct_per_year"].iloc[0]) if not inv_deg.empty else float("nan")
    module = str(inv_deg["module_type"].iloc[0]) if not inv_deg.empty else "—"
    gap_kwh = float(months.loc[months["bucket"] == "DATA_GAP", "lost_kwh"].sum())
    verified_eur = float(months.loc[months["bucket"] != "DATA_GAP", "lost_eur"].sum())
    actionable = float(
        months.loc[months["bucket"].isin(["FAULT", "OUTAGE", "UNDERPERFORMANCE_LOCAL"]), "lost_eur"].sum()
    )

    kpi_row(
        [
            ("Verified loss", money(verified_eur), "all causes · all years", ORANGE),
            ("Actionable loss", money(actionable), "fault + outage + underperf.", "#2F4858"),
            ("Performance trend", f"{slope:.2f} %/yr", "vs year-1 twin · incl. soiling", NAVY),
            ("Module type", module.replace("Module Type", "Type"), "from System_Overview", "#7A9E7E"),
            ("Telemetry gaps", kwh(gap_kwh), "not billed as loss", "#C2C8CE"),
        ]
    )

    with panel("Twin vs reality", f"{inverter} — {month[:7]}", "Orange = measured output · blue band = twin expectation (p10–p90) · dotted lines = service tickets"):
        ts = load_inverter_timeseries(inverter, month)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ts["ts"], y=ts["p90_twin_kw"], line=dict(width=0), name="p90", showlegend=False, hoverinfo="skip"))
        fig.add_trace(
            go.Scatter(
                x=ts["ts"], y=ts["p10_twin_kw"], fill="tonexty",
                fillcolor="rgba(51,101,138,0.16)", line=dict(width=0),
                name="Twin band (p10–p90)", hoverinfo="skip",
            )
        )
        fig.add_trace(go.Scatter(x=ts["ts"], y=ts["p50_twin_kw"], name="Twin expected", line=dict(color=NAVY, width=1.6)))
        fig.add_trace(go.Scatter(x=ts["ts"], y=ts["p_ac_kw"], name="Actual", line=dict(color=ORANGE, width=1.6)))

        tickets = load_inverter_tickets(inverter)
        if not tickets.empty and not ts.empty:
            t0, t1 = ts["ts"].min(), ts["ts"].max()
            in_window = tickets[(tickets["start_ts"] >= t0) & (tickets["start_ts"] <= t1)]
            for _, tk in in_window.iterrows():
                fig.add_vline(x=tk["start_ts"], line_dash="dot", line_color=INK, line_width=1.5)
                fig.add_annotation(x=tk["start_ts"], y=1.02, yref="paper", text="🎫 ticket", showarrow=False, font=dict(size=11, color=INK))
        st.plotly_chart(style_fig(fig, 440), use_container_width=True, config={"displayModeBar": False})

    left, right = st.columns([1.25, 1.0], gap="medium")
    with left:
        with panel("Monthly ledger", f"Where {inverter} lost money"):
            show = pretty_buckets(months).rename(
                columns={
                    "month": "Month", "bucket": "Cause", "lost_eur": "Lost €",
                    "lost_kwh": "Lost kWh", "validated_by_ticket": "Ticket",
                }
            )
            show["Month"] = show["Month"].astype(str).str[:7]
            show["Lost €"] = show["Lost €"].round(0)
            show["Lost kWh"] = show["Lost kWh"].round(0)
            st.dataframe(show.head(18), use_container_width=True, hide_index=True, height=400)
    with right:
        with panel("Fleet context", "This inverter's trend vs the fleet"):
            deg_plot = deg.copy()
            deg_plot["highlight"] = deg_plot["inverter"].eq(inverter).map({True: "Selected", False: "Fleet"})
            fig2 = px.bar(
                deg_plot, x="inverter", y="slope_pct_per_year", color="highlight",
                color_discrete_map={"Selected": ORANGE, "Fleet": "#C7D5E0"},
                labels={"slope_pct_per_year": "%/yr", "inverter": ""},
            )
            fig2.update_layout(showlegend=False, xaxis_showticklabels=False)
            fig2.update_traces(hovertemplate="%{x}: %{y:.2f} %/yr<extra></extra>")
            st.plotly_chart(style_fig(fig2, 400), use_container_width=True, config={"displayModeBar": False})


def ledger_tab() -> None:
    with con() as db:
        years = [r[0] for r in db.execute("SELECT DISTINCT year FROM ledger ORDER BY year").fetchall()]
        buckets = [r[0] for r in db.execute("SELECT DISTINCT bucket FROM ledger ORDER BY bucket").fetchall()]

    f1, f2 = st.columns([1.0, 1.6], gap="small")
    with f1:
        selected_years = st.multiselect("Years", years, default=years[-4:])
    with f2:
        selected_buckets = st.multiselect(
            "Causes", buckets, default=[b for b in buckets if b != "DATA_GAP"],
            format_func=lambda b: BUCKET_LABELS.get(b, b),
        )

    ledger = load_ledger_filtered(selected_years, selected_buckets)
    summary = load_ledger_filter_summary(selected_years, selected_buckets)
    totals = summary["totals"]
    st.markdown(
        f'<div class="chip-row">'
        f'<span class="chip">Filtered total <b>{money(float(totals["lost_eur"]))}</b></span>'
        f'<span class="chip">{int(totals["rows"]):,} matching rows</span>'
        f'<span class="chip">{len(ledger):,} visible rows</span>'
        f'<span class="chip">{int(totals["ticket_rows"])} ticket-validated</span>'
        f"</div>",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1.45, 1.0], gap="medium")
    with c1:
        with panel("Ledger", "Every inverter-month loss line, ranked by €"):
            show = pretty_buckets(ledger).rename(
                columns={
                    "inverter": "Inverter", "month": "Month", "bucket": "Cause",
                    "error_description": "Error", "lost_kwh": "Lost kWh",
                    "lost_eur": "Lost €", "validated_by_ticket": "Ticket",
                }
            )
            show["Month"] = show["Month"].astype(str).str[:7]
            st.dataframe(
                show[["Inverter", "Month", "Cause", "Error", "Lost €", "Lost kWh", "Ticket"]],
                use_container_width=True, hide_index=True, height=560,
            )
    with c2:
        with panel("By cause", "Within current selection"):
            bucket_totals = pretty_buckets(summary["by_bucket"])
            fig = px.bar(
                bucket_totals, x="lost_eur", y="bucket", orientation="h",
                color="bucket", color_discrete_map=LABEL_PALETTE,
                labels={"lost_eur": "Lost €", "bucket": ""},
            )
            fig.update_layout(showlegend=False)
            fig.update_traces(hovertemplate="%{y}: € %{x:,.0f}<extra></extra>")
            st.plotly_chart(style_fig(fig, 220), use_container_width=True, config={"displayModeBar": False})
        with panel("Maintenance budget", "Error codes ranked by € lost — not by count"):
            errors = load_error_ranking().rename(
                columns={"error": "Error", "months_affected": "Months", "lost_eur": "Lost €"}
            )
            st.dataframe(errors, use_container_width=True, hide_index=True, height=250)

    c3, c4 = st.columns([1.0, 1.0], gap="medium")
    with c3:
        with panel("Early-warning record", "Twin flag preceded the service ticket"):
            lead = load_lead_events().rename(
                columns={
                    "event_date": "Flagged", "inverter": "Inverter", "bucket": "Cause",
                    "lost_eur": "€ at flag", "ticket_start_ts": "Ticket opened",
                    "ticket_lag_days": "Lead (days)", "ticket_category": "Ticket category",
                }
            )
            lead["Cause"] = lead["Cause"].map(BUCKET_LABELS).fillna(lead["Cause"])
            lead["Ticket opened"] = lead["Ticket opened"].astype(str).str[:10]
            st.dataframe(lead.head(12), use_container_width=True, hide_index=True, height=380)
    with c4:
        with panel("Procurement signal", "Performance trend by module type (groups with ≥5 inverters)"):
            deg = load_degradation()
            counts = deg["module_type"].value_counts()
            major = {m for m, n in counts.items() if n >= 5}
            deg["group"] = deg["module_type"].where(deg["module_type"].isin(major), "Other (n<5)")
            box = px.box(
                deg, x="group", y="slope_pct_per_year", color="group",
                labels={"slope_pct_per_year": "%/yr (incl. soiling)", "group": ""},
                points="all",
            )
            box.update_layout(showlegend=False)
            st.plotly_chart(style_fig(box, 380), use_container_width=True, config={"displayModeBar": False})


def ask_tab() -> None:
    section(
        "Ask the plant",
        "Ten years of telemetry, in plain language",
        "Answers cite the ledger tables directly. The SQL behind every number is one click away.",
    )
    examples = [
        "Which inverter should we service first and why?",
        "How much did grid curtailment cost us in 2023 vs price curtailment?",
        "Show inverter INV 01.04.023's worst month and what happened.",
    ]
    cols = st.columns(3)
    for idx, example in enumerate(examples):
        if cols[idx].button(example, use_container_width=True, key=f"ex_{idx}"):
            # Example questions answer instantly from the deterministic
            # templates — no LLM round-trip, no quota, no waiting.
            result = answer_with_templates(example, config.DB_PATH)
            history = st.session_state.setdefault("chat_history", [])
            history.append((example, result))
            st.session_state["chat_history"] = history[-6:]

    with st.form("ask_form", clear_on_submit=False):
        question = st.text_input(
            "Question",
            value="",
            label_visibility="collapsed",
            placeholder="e.g. What did degradation cost us in 2024?",
        )
        submitted = st.form_submit_button("Ask SolarTwin", type="primary")

    if submitted and question.strip():
        with st.spinner(
            "Asking SolarTwin with SQL-grounded facts; "
            "falls back to ledger templates if the provider stalls..."
        ):
            result = answer_question(question.strip(), config.DB_PATH)
        history = st.session_state.setdefault("chat_history", [])
        history.append((question.strip(), result))
        st.session_state["chat_history"] = history[-6:]

    for q, result in reversed(st.session_state.get("chat_history", [])):
        with st.chat_message("user", avatar="🧑‍🔧"):
            st.markdown(q)
        with st.chat_message("assistant", avatar="☀️"):
            st.markdown(result.answer)
            if result.rows:
                st.dataframe(pd.DataFrame(result.rows), use_container_width=True, hide_index=True)
            if result.sql:
                with st.expander("Show the SQL behind this answer"):
                    st.code(result.sql, language="sql")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    facts = load_plant_facts()
    kpis = load_kpis()
    render_header(facts)
    render_sidebar(facts, kpis)
    tabs = st.tabs(["Plant Health", "Inverter Deep-Dive", "€ Ledger", "Ask the Plant"])
    with tabs[0]:
        plant_health_tab(kpis)
    with tabs[1]:
        inverter_tab()
    with tabs[2]:
        ledger_tab()
    with tabs[3]:
        ask_tab()


if __name__ == "__main__":
    main()
