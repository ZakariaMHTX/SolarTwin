from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import datetime
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from scipy.stats import theilslopes
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import r2_score

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


FEATURE_COLUMNS = [
    "irradiation_w_m2",
    "altitude_deg",
    "module_temp_c",
    "ambient_temp_c",
    "irradiation_roll15_mean",
    "irradiation_roll15_std",
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "p50_physics_kw",
]


def clip_predictions(values: np.ndarray, kwp: float) -> np.ndarray:
    return np.clip(np.asarray(values, dtype=float), 0.0, float(kwp))


def create_feature_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS twin_features")
    con.execute(
        f"""
        CREATE TABLE twin_features AS
        WITH plant_one AS (
          SELECT
            ts,
            avg(GREATEST(COALESCE(irradiation_w_m2, 0.0), 0.0)) AS irradiation_w_m2,
            avg(COALESCE(altitude_deg, -90.0)) AS altitude_deg,
            avg(COALESCE(module_temp_c, ambient_temp_c, 25.0)) AS module_temp_c,
            avg(COALESCE(ambient_temp_c, module_temp_c, 20.0)) AS ambient_temp_c,
            bool_or(COALESCE(is_curtailed, false)) AS is_curtailed,
            bool_or(COALESCE(is_day, false)) AS is_day
          FROM plant
          GROUP BY ts
        ),
        base AS (
          SELECT
            *,
            EXTRACT(hour FROM ts) * 60 + EXTRACT(minute FROM ts) AS minute_of_day,
            EXTRACT(doy FROM ts) AS day_of_year
          FROM plant_one
        ),
        rolling AS (
          SELECT
            *,
            avg(irradiation_w_m2) OVER (
              ORDER BY ts ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING
            ) AS irradiation_roll15_mean,
            stddev_pop(irradiation_w_m2) OVER (
              ORDER BY ts ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING
            ) AS irradiation_roll15_std
          FROM base
        )
        SELECT
          ts,
          irradiation_w_m2,
          altitude_deg,
          module_temp_c,
          ambient_temp_c,
          COALESCE(irradiation_roll15_mean, irradiation_w_m2) AS irradiation_roll15_mean,
          COALESCE(irradiation_roll15_std, 0.0) AS irradiation_roll15_std,
          sin(2.0 * pi() * minute_of_day / 1440.0) AS hour_sin,
          cos(2.0 * pi() * minute_of_day / 1440.0) AS hour_cos,
          sin(2.0 * pi() * day_of_year / 366.0) AS doy_sin,
          cos(2.0 * pi() * day_of_year / 366.0) AS doy_cos,
          (irradiation_w_m2 / 1000.0)
            * GREATEST(0.65, 1.0 + ({config.TEMP_COEFFICIENT_PER_C})
            * (module_temp_c - 25.0)) AS physics_base_kw_per_kwp,
          is_day,
          is_curtailed,
          (is_day AND irradiation_w_m2 > 20.0) AS is_model_day
        FROM rolling
        ORDER BY ts
        """
    )

    con.execute("DROP TABLE IF EXISTS error_intervals")
    con.execute(
        """
        CREATE TABLE error_intervals AS
        SELECT
          ts,
          inverter,
          min(error_code) AS error_code,
          count(*) AS n_error_rows
        FROM error_events
        GROUP BY 1, 2
        """
    )


def get_training_frame(con: duckdb.DuckDBPyConnection, inverter: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT
          tf.ts,
          tf.irradiation_w_m2,
          tf.altitude_deg,
          tf.module_temp_c,
          tf.ambient_temp_c,
          tf.irradiation_roll15_mean,
          tf.irradiation_roll15_std,
          tf.hour_sin,
          tf.hour_cos,
          tf.doy_sin,
          tf.doy_cos,
          GREATEST(
            0.0,
            LEAST(
              i.kwp,
              i.kwp * tf.physics_base_kw_per_kwp * COALESCE(pe.eta_system, 0.82)
            )
          ) AS p50_physics_kw,
          r.p_ac_kw,
          i.kwp
        FROM readings r
        JOIN twin_features tf USING (ts)
        JOIN inverters i USING (inverter)
        LEFT JOIN physics_eta pe USING (inverter)
        LEFT JOIN error_intervals e USING (ts, inverter)
        WHERE r.inverter = ?
          AND r.ts >= TIMESTAMP '2017-01-01'
          AND r.ts < TIMESTAMP '2018-01-01'
          AND tf.is_model_day
          AND tf.irradiation_w_m2 > 50
          AND tf.is_curtailed = false
          AND e.error_code IS NULL
          AND r.p_ac_kw IS NOT NULL
          AND r.p_ac_kw > 0
        ORDER BY r.ts
        """,
        [inverter],
    ).fetchdf()


def fit_one_model(train_df: pd.DataFrame, inverter: str, kwp: float) -> tuple[HistGradientBoostingRegressor, dict]:
    if len(train_df) < 2_000:
        raise RuntimeError(f"{inverter}: only {len(train_df)} clean training rows")

    split_ts = pd.Timestamp("2017-12-01")
    train_mask = train_df["ts"] < split_ts
    valid_mask = train_df["ts"] >= split_ts
    if train_mask.sum() < 1_000 or valid_mask.sum() < 200:
        split_idx = max(1_000, int(len(train_df) * 0.8))
        train_mask = np.zeros(len(train_df), dtype=bool)
        train_mask[:split_idx] = True
        valid_mask = ~train_mask

    fit_df = train_df.loc[train_mask]
    valid_df = train_df.loc[valid_mask]
    if len(valid_df) < 100:
        valid_df = train_df.iloc[-min(2_000, len(train_df)) :]

    model = HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=0.08,
        max_iter=180,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=0.02,
        early_stopping=True,
        validation_fraction=0.12,
        n_iter_no_change=15,
        random_state=config.RANDOM_SEED,
    )
    model.fit(fit_df[FEATURE_COLUMNS], fit_df["p_ac_kw"])

    pred = clip_predictions(model.predict(valid_df[FEATURE_COLUMNS]), kwp)
    actual = valid_df["p_ac_kw"].to_numpy(dtype=float)
    residual = actual - pred

    rmse = float(np.sqrt(np.mean((pred - actual) ** 2)))
    nrmse = rmse / kwp if kwp else math.nan
    mbe = float(np.mean(pred - actual)) / kwp if kwp else math.nan
    denom = float(np.sum(actual))
    energy_abs_error = float(np.sum(np.abs(pred - actual)) / denom) if denom > 0 else math.nan
    r2 = float(r2_score(actual, pred)) if len(valid_df) > 1 else math.nan
    q10 = float(np.quantile(residual, 0.10))
    q90 = float(np.quantile(residual, 0.90))
    p10 = np.minimum(pred, clip_predictions(pred + q10, kwp))
    p90 = np.maximum(pred, clip_predictions(pred + q90, kwp))
    coverage = float(np.mean((actual >= p10) & (actual <= p90)))

    metrics = {
        "inverter": inverter,
        "kwp": float(kwp),
        "n_rows_clean_2017": int(len(train_df)),
        "n_train": int(len(fit_df)),
        "n_valid": int(len(valid_df)),
        "nrmse": float(nrmse),
        "rmse_kw": rmse,
        "mbe_norm": float(mbe),
        "r2": r2,
        "energy_weighted_abs_error": energy_abs_error,
        "residual_q10_kw": q10,
        "residual_q90_kw": q90,
        "band_coverage": coverage,
        "model_iterations": int(getattr(model, "n_iter_", 0)),
    }
    return model, metrics


def predict_and_insert(
    con: duckdb.DuckDBPyConnection,
    model: HistGradientBoostingRegressor,
    inverter: str,
    kwp: float,
    eta_system: float,
    base_features: pd.DataFrame,
    base_feature_values: np.ndarray,
    residual_q10: float,
    residual_q90: float,
) -> None:
    physics = clip_predictions(
        base_features["physics_base_kw_per_kwp"].to_numpy(dtype=float) * kwp * eta_system,
        kwp,
    )
    x = np.column_stack([base_feature_values, physics])
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names")
        p50 = clip_predictions(model.predict(x), kwp)

    night_mask = ~base_features["is_model_day"].to_numpy(dtype=bool)
    p50[night_mask] = 0.0

    p10 = np.minimum(p50, clip_predictions(p50 + residual_q10, kwp))
    p90 = np.maximum(p50, clip_predictions(p50 + residual_q90, kwp))
    p10[night_mask] = 0.0
    p90[night_mask] = 0.0

    pred_df = pd.DataFrame(
        {
            "ts": base_features["ts"],
            "inverter": inverter,
            "p10_twin_kw": p10,
            "p50_twin_kw": p50,
            "p90_twin_kw": p90,
        }
    )
    con.register("pred_batch", pred_df)
    con.execute("INSERT INTO twin_predictions SELECT * FROM pred_batch")
    con.unregister("pred_batch")


def create_monthly_degradation_tables(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, pd.DataFrame]:
    con.execute("DROP TABLE IF EXISTS twin_monthly_health")
    con.execute(
        """
        CREATE TABLE twin_monthly_health AS
        SELECT
          date_trunc('month', r.ts)::DATE AS month,
          r.inverter,
          any_value(i.module_type) AS module_type,
          any_value(i.kwp) AS kwp,
          count(*) AS n_points,
          sum(GREATEST(r.p_ac_kw, 0) * (5.0 / 60.0)) AS actual_kwh,
          sum(GREATEST(tp.p50_twin_kw, 0) * (5.0 / 60.0)) AS expected_kwh,
          actual_kwh / NULLIF(expected_kwh, 0) AS energy_ratio,
          median(r.p_ac_kw / NULLIF(tp.p50_twin_kw, 0)) AS median_ratio
        FROM readings r
        JOIN twin_predictions tp USING (ts, inverter)
        JOIN twin_features tf USING (ts)
        JOIN inverters i USING (inverter)
        LEFT JOIN error_intervals e USING (ts, inverter)
        WHERE tf.is_day
          AND tf.irradiation_w_m2 > 100
          AND tf.is_curtailed = false
          AND e.error_code IS NULL
          AND r.p_ac_kw IS NOT NULL
          AND r.p_ac_kw > 0
          AND tp.p50_twin_kw > 0.10 * i.kwp
        GROUP BY 1, 2
        HAVING count(*) >= 100
        ORDER BY 1, 2
        """
    )

    monthly = con.execute(
        """
        SELECT *
        FROM twin_monthly_health
        WHERE month >= DATE '2018-01-01'
          AND median_ratio IS NOT NULL
        ORDER BY inverter, month
        """
    ).fetchdf()

    rows: list[dict] = []
    for inverter, group in monthly.groupby("inverter"):
        group = group.sort_values("month")
        if len(group) < 12:
            continue
        x = (group["month"] - group["month"].min()).dt.days.to_numpy(dtype=float) / 365.25
        y = group["median_ratio"].to_numpy(dtype=float)
        good = np.isfinite(x) & np.isfinite(y) & (y > 0)
        if good.sum() < 12:
            continue
        slope, intercept, low, high = theilslopes(y[good], x[good], alpha=0.90)
        rows.append(
            {
                "inverter": inverter,
                "module_type": group["module_type"].iloc[0],
                "n_months": int(good.sum()),
                "first_month": str(group.loc[good, "month"].min()),
                "last_month": str(group.loc[good, "month"].max()),
                "start_ratio": float(y[good][0]),
                "end_ratio": float(y[good][-1]),
                "slope_ratio_per_year": float(slope),
                "slope_pct_per_year": float(slope * 100.0),
                "ci_low_pct_per_year": float(low * 100.0),
                "ci_high_pct_per_year": float(high * 100.0),
                "intercept_ratio": float(intercept),
            }
        )

    slopes = pd.DataFrame(rows)
    if slopes.empty:
        slopes = pd.DataFrame(
            columns=[
                "inverter",
                "module_type",
                "n_months",
                "first_month",
                "last_month",
                "start_ratio",
                "end_ratio",
                "slope_ratio_per_year",
                "slope_pct_per_year",
                "ci_low_pct_per_year",
                "ci_high_pct_per_year",
                "intercept_ratio",
            ]
        )

    con.execute("DROP TABLE IF EXISTS degradation_slopes")
    con.register("degradation_slopes_df", slopes)
    con.execute("CREATE TABLE degradation_slopes AS SELECT * FROM degradation_slopes_df")
    con.unregister("degradation_slopes_df")

    if slopes.empty:
        module = pd.DataFrame(columns=["module_type", "n_inverters", "median_slope_pct_per_year"])
    else:
        module = (
            slopes.groupby("module_type", dropna=False)
            .agg(
                n_inverters=("inverter", "count"),
                median_slope_pct_per_year=("slope_pct_per_year", "median"),
                mean_slope_pct_per_year=("slope_pct_per_year", "mean"),
                p10_slope_pct_per_year=("slope_pct_per_year", lambda s: float(np.quantile(s, 0.10))),
                p90_slope_pct_per_year=("slope_pct_per_year", lambda s: float(np.quantile(s, 0.90))),
            )
            .reset_index()
        )

    con.execute("DROP TABLE IF EXISTS degradation_module_type")
    con.register("degradation_module_type_df", module)
    con.execute("CREATE TABLE degradation_module_type AS SELECT * FROM degradation_module_type_df")
    con.unregister("degradation_module_type_df")
    return slopes, module


def write_summary(metrics: dict) -> None:
    lines = [
        "# SolarTwin Phase 3 ML Twin Summary",
        "",
        f"Generated: `{metrics['generated_at']}`",
        "",
        "## Tables Created",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ]
    for table, count in metrics["counts"].items():
        lines.append(f"| `{table}` | {count:,} |")

    eval_metrics = metrics["evaluation"]
    lines.extend(
        [
            "",
            "## Model",
            "",
            "- One `HistGradientBoostingRegressor` median model per inverter.",
            "- Features are environment-only plus the physics baseline: irradiance, altitude, module/ambient temperature, 15-minute irradiance rolling statistics, hour/day cyclic features, and `p50_physics_kw`.",
            "- No inverter electrical lag features are used, so the model cannot learn faults from its own future target.",
            "- Bands are calibrated from held-out residual quantiles within year 1: p10/p90 are residual-adjusted around p50.",
            "",
            "## Held-out Evaluation",
            "",
            f"- Median nRMSE: `{eval_metrics['median_nrmse']:.3f}` of inverter kWp.",
            f"- Median R2: `{eval_metrics['median_r2']:.3f}`.",
            f"- Median energy-weighted absolute error: `{eval_metrics['median_energy_weighted_abs_error']:.3f}`.",
            f"- Median p10-p90 band coverage: `{eval_metrics['median_band_coverage']:.3f}`.",
            "",
            "## Degradation",
            "",
            f"- Inverters with degradation slopes: `{metrics['degradation']['n_inverters']}`.",
            f"- Median slope: `{metrics['degradation']['median_slope_pct_per_year']:.3f}` %/year.",
            f"- Slope range: `{metrics['degradation']['min_slope_pct_per_year']:.3f}` to `{metrics['degradation']['max_slope_pct_per_year']:.3f}` %/year.",
            "",
            "## Caveat",
            "",
            "The twin treats year 1 as the reference healthy state. If an inverter was already degraded in year 1, the estimated losses are conservative.",
        ]
    )
    (config.OUTPUT_DIR / "twin_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-inverters", type=int, default=None)
    args = parser.parse_args()

    model_dir = config.OUTPUT_DIR / "models"
    model_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(config.DB_PATH))
    con.execute("PRAGMA threads=4")
    con.execute("PRAGMA memory_limit='10GB'")

    print("Creating reusable twin feature tables...")
    create_feature_tables(con)

    inverters_df = con.execute(
        """
        SELECT
          i.inverter,
          i.kwp,
          i.module_type,
          COALESCE(pe.eta_system, 0.82) AS eta_system
        FROM inverters i
        LEFT JOIN physics_eta pe USING (inverter)
        WHERE kwp IS NOT NULL AND kwp > 0
        ORDER BY inverter
        """
    ).fetchdf()
    if args.limit_inverters:
        inverters_df = inverters_df.head(args.limit_inverters)

    print("Loading base environmental feature frame...")
    base_features = con.execute(
        """
        SELECT
          ts,
          irradiation_w_m2,
          altitude_deg,
          module_temp_c,
          ambient_temp_c,
          irradiation_roll15_mean,
          irradiation_roll15_std,
          hour_sin,
          hour_cos,
          doy_sin,
          doy_cos,
          physics_base_kw_per_kwp,
          is_model_day
        FROM twin_features
        ORDER BY ts
        """
    ).fetchdf()
    base_feature_values = base_features[
        [
            "irradiation_w_m2",
            "altitude_deg",
            "module_temp_c",
            "ambient_temp_c",
            "irradiation_roll15_mean",
            "irradiation_roll15_std",
            "hour_sin",
            "hour_cos",
            "doy_sin",
            "doy_cos",
        ]
    ].to_numpy(dtype=float)

    con.execute("DROP TABLE IF EXISTS twin_predictions")
    con.execute(
        """
        CREATE TABLE twin_predictions (
          ts TIMESTAMP,
          inverter VARCHAR,
          p10_twin_kw DOUBLE,
          p50_twin_kw DOUBLE,
          p90_twin_kw DOUBLE
        )
        """
    )

    metrics_rows: list[dict] = []
    for idx, row in inverters_df.iterrows():
        inverter = row["inverter"]
        kwp = float(row["kwp"])
        eta_system = float(row["eta_system"])
        print(f"[{idx + 1}/{len(inverters_df)}] Training {inverter}...")
        train_df = get_training_frame(con, inverter)
        model, metrics = fit_one_model(train_df, inverter, kwp)
        model_path = model_dir / f"{inverter.replace(' ', '_').replace('.', '_')}_p50.joblib"
        joblib.dump(model, model_path)
        metrics["model_path"] = str(model_path)
        metrics_rows.append(metrics)

        print(f"[{idx + 1}/{len(inverters_df)}] Replaying {inverter}...")
        predict_and_insert(
            con,
            model,
            inverter,
            kwp,
            eta_system,
            base_features,
            base_feature_values,
            metrics["residual_q10_kw"],
            metrics["residual_q90_kw"],
        )

    metrics_df = pd.DataFrame(metrics_rows)
    con.execute("DROP TABLE IF EXISTS twin_model_metrics")
    con.register("twin_model_metrics_df", metrics_df)
    con.execute("CREATE TABLE twin_model_metrics AS SELECT * FROM twin_model_metrics_df")
    con.unregister("twin_model_metrics_df")

    print("Computing monthly degradation tables...")
    slopes, module = create_monthly_degradation_tables(con)

    counts = {
        "twin_features": con.execute("SELECT COUNT(*) FROM twin_features").fetchone()[0],
        "error_intervals": con.execute("SELECT COUNT(*) FROM error_intervals").fetchone()[0],
        "twin_predictions": con.execute("SELECT COUNT(*) FROM twin_predictions").fetchone()[0],
        "twin_model_metrics": con.execute("SELECT COUNT(*) FROM twin_model_metrics").fetchone()[0],
        "twin_monthly_health": con.execute("SELECT COUNT(*) FROM twin_monthly_health").fetchone()[0],
        "degradation_slopes": con.execute("SELECT COUNT(*) FROM degradation_slopes").fetchone()[0],
        "degradation_module_type": con.execute("SELECT COUNT(*) FROM degradation_module_type").fetchone()[0],
    }

    evaluation = {
        "median_nrmse": float(metrics_df["nrmse"].median()),
        "mean_nrmse": float(metrics_df["nrmse"].mean()),
        "max_nrmse": float(metrics_df["nrmse"].max()),
        "median_r2": float(metrics_df["r2"].median()),
        "median_energy_weighted_abs_error": float(metrics_df["energy_weighted_abs_error"].median()),
        "median_band_coverage": float(metrics_df["band_coverage"].median()),
    }
    if slopes.empty:
        degradation = {
            "n_inverters": 0,
            "median_slope_pct_per_year": 0.0,
            "min_slope_pct_per_year": 0.0,
            "max_slope_pct_per_year": 0.0,
        }
    else:
        degradation = {
            "n_inverters": int(len(slopes)),
            "median_slope_pct_per_year": float(slopes["slope_pct_per_year"].median()),
            "min_slope_pct_per_year": float(slopes["slope_pct_per_year"].min()),
            "max_slope_pct_per_year": float(slopes["slope_pct_per_year"].max()),
        }

    metrics = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model_type": "sklearn HistGradientBoostingRegressor median model with held-out residual bands",
        "feature_columns": FEATURE_COLUMNS,
        "counts": {k: int(v) for k, v in counts.items()},
        "evaluation": evaluation,
        "degradation": degradation,
        "module_type_degradation": module.to_dict(orient="records"),
    }
    (config.OUTPUT_DIR / "twin_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    write_summary(metrics)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
