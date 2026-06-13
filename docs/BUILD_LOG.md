# SolarTwin Build Log

This file documents each completed build step for the SolarTwin project.

Project root: `solartwin/` (repository root)

Raw dataset root: `../EP-Challenge-Final -` (restricted; not included in this repo)

Data-use constraint: raw Enerparc data is local and restricted to hackathon use. The project must not upload or commit raw data or row-level extracts to public services.

## Step 0 — Project Skeleton

**Status:** Done

**Goal:** Create a contained local project folder for code, outputs, figures, and documentation while leaving the Enerparc raw dataset untouched.

**Created:**

- `solartwin/README.md`
- `solartwin/requirements.txt`
- `solartwin/src/solartwin/`
- `solartwin/scripts/`
- `solartwin/app/`
- `solartwin/outputs/figures/`
- `solartwin/pitch/`
- `solartwin/docs/BUILD_LOG.md`

**Implementation notes:**

- The live agent will use OpenAI later because the user will provide `OPENAI_API_KEY` at the end.
- The core pipeline does not require any API key.
- Python 3.12 will be used instead of the system default Python 3.14 for better package compatibility.

**Verification:**

- Project directories were created successfully.
- Dataset remains in its original location.

## Step 1 — Local Python Environment

**Status:** Done

**Goal:** Create a reproducible local Python environment for DuckDB, Parquet/Excel ingestion, modeling, plotting, Streamlit, and the later OpenAI agent.

**Environment:**

- Virtual environment: `solartwin/.venv`
- Interpreter used by scripts: `solartwin/.venv/bin/python`
- Python version: 3.12.11
- Note: the shell command `python` still resolves to system Python 2.7 on this Mac, so scripts and run commands must use `.venv/bin/python` explicitly.

**Installed packages:**

- Data/storage: `duckdb`, `pandas`, `pyarrow`, `polars`
- Modeling: `numpy`, `scikit-learn`, `scipy`, `pvlib`
- Excel/PDF-adjacent file support: `openpyxl`, `pyxlsb`
- Dashboard/figures: `streamlit`, `plotly`, `kaleido`
- Later agent integration: `openai`

**Verification:**

- Core imports passed: `duckdb`, `pandas`, `pyarrow`, `polars`, `sklearn`, `pvlib`, `openpyxl`, `pyxlsb`.
- Dashboard imports passed: `streamlit`, `plotly`.
- No API key is needed for Phases 0-4.

**Disk note:**

- The virtual environment uses about 1 GB.
- Disk remains tight but workable for the MVP; generated row-level artifacts must be kept compact.

## Step 2 — Phase 0 Data Inspection

**Status:** Done

**Script:** `solartwin/scripts/00_inspect.py`

**Outputs:**

- `solartwin/outputs/data_facts.md`
- `solartwin/outputs/data_facts.json`

**Goal:** Verify that the required Enerparc inputs exist and inspect the main schema, timestamps, inverter columns, key plant signals, error-code file, workbooks, and data-use policy before building the database.

**What was used:**

- `main_monitoring_data.parquet`
- `errorcodes.parquet`
- `main_monitoring_data_legend.xlsb`
- `System_Overview.xlsx`
- `Tickets.xlsx`
- `feed-in-tarrifs.xlsx`
- `errorcodes description (important).xlsx`
- `(please read first) General information plant A.pdf`
- `Data Use Policy.txt`

**Findings:**

- Main monitoring data: `990,442` rows × `206` columns.
- Resolution: median `5.0` minutes.
- Time span: `2016-12-31 22:00:00` to `2026-06-01 21:55:00`.
- Detected `65` inverters from `INV 01.01.001` to `INV 01.09.065`.
- Error-code data: `990,442` rows × `131` columns, also covering `65` inverters.
- Key signals exist: `P_AC`, `I_DC_SUM`, `U_DC`, irradiation, altitude, ambient/module temperature, EVU, DV, and Janitza grid tracks.
- Janitza plant `P_AC` appears negative during feed-in, so plant-vs-inverter sanity checks must compare `sum(inverter P_AC)` to `-Janitza P_AC`.
- The data-use policy confirms the data is non-public and restricted to hackathon use.

**Implementation notes:**

- The Parquet stores `timestamp` as a pandas index field; Phase 0 handles this explicitly.
- Full timezone/location verification remains for the physics layer because it needs solar-position comparison.
- EVU/DV semantics require capacity-normalized checks after metadata ingest.

**Verification:**

- `outputs/data_facts.md` and `outputs/data_facts.json` were written successfully.
- Disk impact was small: `outputs/` was about 108 KB after this phase.

## Step 3 — Phase 1 DuckDB Ingest

**Status:** Done

**Script:** `solartwin/scripts/01_ingest.py`

**Outputs:**

- `solartwin/outputs/solartwin.duckdb`
- `solartwin/outputs/ingest_summary.md`
- `solartwin/outputs/ingest_metrics.json`

**Goal:** Convert raw wide Enerparc files into a local DuckDB star schema that later layers can query consistently.

**Tables created:**

- `plant`: timestamp-level plant/environment tracks.
- `readings`: long inverter telemetry table with one row per timestamp and inverter.
- `inverters`: normalized inverter metadata from `System_Overview.xlsx`.
- `tariffs_weekly`: inverter-level weekly feed-in tariffs.
- `tariffs_monthly`: inverter-level monthly average feed-in tariffs.
- `tickets`: normalized service tickets from both ticket sheets.
- `error_catalog`: error code descriptions with a simple severity class.
- `error_events`: sparse non-zero inverter error-code events.
- `fleet_5min`: fleet median/average per-kWp output for daylight timestamps.
- `readings_monthly`: pre-aggregated monthly energy and reporting counts.
- `readings_with_context`: convenience view joining readings, plant, and inverter metadata.

**What was used:**

- Main monitoring Parquet for telemetry.
- Errorcodes Parquet for non-zero error events.
- System overview workbook for inverter capacities, module types, manufacturers, strings, and combiner groups.
- Feed-in tariff workbook for weekly and monthly tariff tables.
- Ticket workbook for maintenance/service validation later.
- Error-code description workbook for human-readable fault explanations.

**Key results:**

- `plant`: `990,442` rows.
- `readings`: `64,378,730` rows.
- `inverters`: `65` rows.
- `error_events`: `439,010` rows.
- `tickets`: `84` rows.
- `tariffs_monthly`: `8,580` rows.
- Parsed total capacity: `1,897.30` kWp.

**Sanity checks:**

- Rows where inverter `P_AC > 1.1 × kWp`: `0`.
- Sample week `2018-06-01` to `2018-06-08`:
  - Average summed inverter power: `612.83` kW.
  - Average Janitza feed-in after sign correction: `604.23` kW.
  - Average relative gap: `2.07%`.

**Implementation notes:**

- Janitza `P_AC` uses the opposite sign convention from inverter output; feed-in is represented as negative plant power.
- `error_events` stores non-zero error codes only to keep the table sparse.
- The large `readings` table is materialized once so downstream layers do not repeatedly unpivot the wide Parquet.
- The database was about 544 MB after ingest, much smaller than expected and safe for the local disk.

## Step 4 — Phase 2 Physics Baseline

**Status:** Done

**Script:** `solartwin/scripts/02_physics.py`

**Outputs:**

- `solartwin/outputs/physics_summary.md`
- `solartwin/outputs/physics_metrics.json`
- Additional DuckDB tables inside `solartwin/outputs/solartwin.duckdb`

**Goal:** Build an explainable first-principles baseline for expected inverter AC power using measured irradiance, module temperature, inverter capacity, and a fitted per-inverter system efficiency factor.

**Tables created:**

- `physics_eta`: one fitted `eta_system` value per inverter.
- `physics_predictions`: 5-minute expected power bands for every inverter timestamp.
- `performance_ratio_monthly`: monthly actual energy divided by plane-of-array reference energy.
- `physics_monthly`: monthly actual-vs-expected energy and peer-ratio helpers.

**Formula used:**

`P_expected = kWp × (irradiation / 1000) × [1 + gamma × (module_temperature - 25)] × eta_system`

where:

- `gamma = -0.004` per °C.
- `eta_system` is fitted per inverter from 2017 daylight, high-irradiance, non-curtailed operating points.
- Predicted power is clipped to `0 <= P_expected <= kWp`.
- The MVP prediction interval uses `p10 = 0.90 × p50` and `p90 = 1.10 × p50`.

**What was used:**

- `readings` for actual inverter `P_AC`.
- `plant` for irradiation, altitude, module/ambient temperature, and curtailment flags.
- `inverters` for kWp capacity.
- `fleet_5min` for peer-normalized production ratios.

**Key results:**

- `physics_eta`: `65` rows.
- `physics_predictions`: `64,380,030` rows.
- `performance_ratio_monthly`: `7,299` rows.
- `physics_monthly`: `7,475` rows.
- Median fitted eta: `0.874`.
- Eta range: `0.797` to `0.942`.
- Median training points per inverter: `21,085`.
- Median monthly performance ratio: `0.771`.
- Median monthly peer ratio: `1.000`.

**Timestamp note:**

- In June 2018 the daily solar-altitude maximum occurs at about `11:08` in the source timestamps.
- This is consistent with UTC-like timestamps for a German plant during summer daylight-saving time.
- The MVP keeps timestamps as provided because the measured irradiance/temperature baseline does not require absolute timezone conversion.

**Verification:**

- All 65 inverters received a fitted physics efficiency factor.
- The physics prediction table covers the same row count as the long inverter readings table.
- The database grew to about 2.4 GB after this phase, leaving about 15 GB free on the local disk.

## Step 5 — Phase 3 Learned Digital Twin

**Status:** Done

**Script:** `solartwin/scripts/03_twin.py`

**Outputs:**

- `solartwin/outputs/twin_summary.md`
- `solartwin/outputs/twin_metrics.json`
- `solartwin/outputs/models/*.joblib`
- Additional DuckDB tables inside `solartwin/outputs/solartwin.duckdb`

**Goal:** Train a year-1 reference twin for every inverter, replay the full 2017-2026 horizon, and create calibrated prediction bands plus degradation slopes for the attribution layer.

**Tables created:**

- `twin_features`: canonical one-row-per-timestamp environmental feature frame.
- `error_intervals`: exact timestamp/inverter error helper.
- `twin_predictions`: p10/p50/p90 learned-twin power bands for every inverter and canonical timestamp.
- `twin_model_metrics`: held-out model quality metrics per inverter.
- `twin_monthly_health`: clean monthly actual-vs-twin ratios.
- `degradation_slopes`: Theil-Sen degradation slope per inverter.
- `degradation_module_type`: module-type aggregation of degradation slopes.

**What was used:**

- `readings` for inverter target power.
- `twin_features` from `plant` for environment-only inputs.
- `physics_eta` and inverter `kWp` for the physics-informed `p50_physics_kw` feature.
- `error_intervals` and curtailment flags to remove visibly unhealthy/curtailed training points.
- `scikit-learn` `HistGradientBoostingRegressor` for the p50 learned twin.
- `scipy.stats.theilslopes` for robust degradation trend extraction.

**Model design:**

- One median model per inverter, trained on clean 2017 operating points.
- Features: irradiance, sun altitude, module temperature, ambient temperature, 15-minute irradiance rolling mean/std, hour/day cyclic features, and physics baseline power.
- No inverter electrical lag features were used, so the twin does not learn from the faulty signal it is supposed to detect.
- p10/p90 bands were calibrated from held-out residual quantiles inside year 1.

**Duplicate timestamp fix:**

- The source data has 10 duplicated annual-midnight timestamps.
- Phase 3 uses a canonical `twin_features` table with one row per timestamp (`990,432` distinct timestamps) to avoid join multiplication.
- `twin_predictions` therefore has `64,378,080` rows (`990,432 × 65`), and it joins back to duplicated readings without creating four-way duplicate rows.

**Key results:**

- `twin_features`: `990,432` rows.
- `twin_predictions`: `64,378,080` rows.
- `twin_model_metrics`: `65` rows.
- `twin_monthly_health`: `6,591` rows.
- `degradation_slopes`: `65` rows.
- `degradation_module_type`: `20` rows.
- Median held-out nRMSE: `4.22%` of inverter kWp.
- Mean held-out nRMSE: `4.43%`.
- Worst held-out nRMSE: `7.96%` (`INV 01.03.016`).
- Median held-out R2: `0.671`.
- Median p10-p90 coverage: `79.9%`.
- Median degradation slope: `-1.084%/year`.
- Degradation slope range: `-1.943%/year` to `-0.379%/year`.

**Implementation notes:**

- The model files use about 40 MB.
- The DuckDB file grew to about 3.2 GB after this phase.
- The twin treats 2017 as the healthy reference year; if an inverter was already degraded in 2017, later loss estimates are conservative.

**Verification:**

- All 65 inverters trained and replayed successfully.
- The full replay completed on the local Mac with no API key.
- The median accuracy is inside the target band; a few worst-case inverters are documented instead of hidden.

## Step 6 — Phase 4 Attribution Engine and EUR Loss Ledger

**Status:** Done

**Script:** `solartwin/scripts/04_attribution_ledger.py`

**Outputs:**

- `solartwin/outputs/ledger.csv`
- `solartwin/outputs/ledger_by_year_cause.csv`
- `solartwin/outputs/ledger_top20_inverter_cause.csv`
- `solartwin/outputs/ledger_module_type_year.csv`
- `solartwin/outputs/ticket_validation_events.csv`
- `solartwin/outputs/ticket_lead_events.csv`
- `solartwin/outputs/ledger_summary.md`
- `solartwin/outputs/ledger_metrics.json`

**Goal:** Convert every material daytime shortfall into a single cause bucket, convert kWh into euros using inverter/month tariffs, split degradation out of local underperformance, and validate loss rows against service tickets.

**Tables created:**

- `plant_controls`: canonical one-row-per-timestamp DV/EVU controls.
- `error_context_15min`: error-code context expanded to ±15 minutes.
- `loss_intervals`: interval-level classified losses.
- `interval_ledger_raw`: raw monthly aggregation before degradation splitting.
- `twin_expected_monthly`: monthly expected daylight energy.
- `underperformance_monthly_raw`: local-underperformance pool available for degradation splitting.
- `degradation_monthly_loss`: monthly degradation trend loss from the twin residual slope.
- `interval_ledger_adjusted`: interval ledger after subtracting the degradation share.
- `ledger`: final ticket-validated deliverable table.
- `ticket_validation_events`: top daily loss events with nearest ticket match.
- `ticket_lead_events`: ticket-linked examples where model flags occur before the ticket.

**Classification ladder:**

1. `CURTAILMENT_GRID`: `EVU < 99.5%`.
2. `CURTAILMENT_PRICE`: `DV < 99.5%`.
3. `FAULT`: inverter error code within ±15 minutes.
4. `DATA_GAP`: inverter telemetry missing while peers and the Janitza plant meter indicate the plant was still producing.
5. `OUTAGE`: actual power missing/near zero while peers are producing and the Janitza plant meter corroborates a production shortfall.
6. `UNDERPERFORMANCE_LOCAL`: persistent below-p10 behavior (`>=9` of `13` intervals in a ±30 minute window) without stronger context.
7. `DEGRADATION`: monthly Theil-Sen residual trend split out of local underperformance.

**What was used:**

- `readings`, `twin_predictions`, and `twin_features` for actual-vs-twin shortfall.
- `plant_controls` for grid/operator curtailment split.
- `error_context_15min` and `error_catalog` for fault attribution.
- `fleet_5min` for outage/peer context.
- `tariffs_monthly` for euro conversion.
- `tickets` for validation flags and positive lead-time examples.

**Key results:**

- `loss_intervals`: `5,913,261` rows.
- `ledger`: `22,210` rows.
- Verified production loss: `1,266,258` kWh.
- Verified production loss value: `EUR 167,715`.
- Telemetry `DATA_GAP`: `960,570` kWh, about `EUR 126,694`, excluded from the verified-loss headline.
- Ticket-validated monthly rows: `588`.
- Positive lead-time ticket examples: `50`.
- Median positive lead time among exported ticket examples: `15.0` days.
- Max positive lead time among exported ticket examples: `28` days.

**Loss by bucket:**

| Bucket | Lost kWh | Lost EUR |
|---|---:|---:|
| `DATA_GAP` | 960,570 | 126,694 |
| `DEGRADATION` | 746,858 | 98,046 |
| `FAULT` | 233,721 | 29,883 |
| `CURTAILMENT_PRICE` | 126,911 | 18,921 |
| `UNDERPERFORMANCE_LOCAL` | 118,227 | 15,688 |
| `OUTAGE` | 39,983 | 5,111 |
| `CURTAILMENT_GRID` | 557 | 66 |

**Accounting check:**

- Raw interval loss: `1,883,956.87` kWh.
- Adjusted interval loss after removing degradation overlap: `1,479,970.19` kWh.
- Degradation trend loss: `746,858.20` kWh.
- Ledger total including `DATA_GAP`: `2,226,828.39` kWh.
- Difference: `0.0000%`.

**Implementation notes:**

- The final ledger uses canonical timestamp tables to avoid duplicate joins from annual-midnight timestamp duplicates in the raw data.
- `DATA_GAP` is deliberately not counted as verified production loss; it is a monitoring-availability finding.
- Degradation is not capped at below-p10 underperformance because slow performance drift often remains inside the p10-p90 band; overlap with local underperformance is subtracted to avoid double counting.
- The database was about 3.7 GB after this rerun.

**Verification:**

- All requested production-loss buckets plus `DATA_GAP` appear in `ledger.csv`.
- The no-double-counting accounting check passes.
- Ticket-linked lead-time examples were exported for pitch case studies.

## Step 7 — Phase 5 Dashboard and Agent Shell

**Status:** Done

**Files:**

- `solartwin/app/dashboard.py`
- `solartwin/src/solartwin/agent.py`

**Goal:** Expose the digital twin and verified-loss ledger as a live O&M dashboard with a local question-answering agent that works without an API key and upgrades to read-only SQL tool use when a configured provider key is available.

**Dashboard tabs:**

- `Plant Health`: KPI strip, yearly stacked verified loss by cause, inverter-by-month verified-loss heatmap, and separate telemetry-gap KPI.
- `Inverter Deep-Dive`: selected inverter ledger rows, degradation slope, actual-vs-twin-band time series for the worst month.
- `EUR Ledger`: filtered ledger table, bucket totals, ticket lead-time examples, degradation slope chart.
- `Ask the Plant`: deterministic fallback answers for rehearsed demo questions and optional OpenAI-compatible SQL tool loop.

**Agent tools implemented:**

- `get_schema()`: returns available DuckDB tables, columns, and semantics.
- `run_sql(query)`: read-only `SELECT`/`WITH` queries, rejects write/admin keywords, caps output at 200 rows.
- `lookup_error_code(code)`: maps raw inverter error codes to the base error-code catalog.

**What was used:**

- `streamlit` for the app.
- `plotly` for charts.
- `duckdb` read-only connections for all dashboard and agent data access.
- `openai` package for OpenAI-compatible providers. Provider order is OpenAI, then Gemini, then NVIDIA MiniMax M3, based on configured environment keys.

**Verification:**

- `src/solartwin/agent.py` and `app/dashboard.py` compile successfully.
- The live provider was switched to OpenAI first because MiniMax M3 was valid but slow for this demo path.
- The deterministic agent answered: service priority is `INV 01.08.058`, with `EUR 3,664` actionable loss and `30,429` kWh. Curtailment, degradation, and telemetry gaps are excluded from this service ranking.
- Streamlit was tested successfully at `http://127.0.0.1:8521`.
- Streamlit health endpoint returned `ok`.
- Dashboard data loaders returned KPI, yearly rollup, inverter list, ticket lead-event data, and full filtered ledger totals from DuckDB.

**Limitation:**

- Visual screenshot verification was completed with local headless Chrome via DevTools protocol. The main dashboard values matched direct SQL checks.

## Step 8 — Phase 6 Pitch Figures and Narrative

**Status:** Done

**Script:** `solartwin/scripts/06_report.py`

**Outputs:**

- `solartwin/outputs/figures/01_twin_band_vs_actual.png`
- `solartwin/outputs/figures/02_fleet_loss_heatmap.png`
- `solartwin/outputs/figures/03_degradation_slopes.png`
- `solartwin/outputs/figures/04_stacked_eur_loss_by_year.png`
- `solartwin/outputs/figures/05_ticket_lead_time_case.png`
- `solartwin/outputs/figures/06_heldout_accuracy.png`
- Matching `.html` versions for all six figures.
- `solartwin/outputs/figures_manifest.json`
- `solartwin/outputs/final_metrics.json`
- `solartwin/pitch/PITCH.md`

**Goal:** Produce static fallback assets and a filled five-minute pitch narrative from the real ledger/twin metrics.

**Figures created:**

1. Twin band vs actual for a ticket-linked inverter case.
2. Fleet heatmap of inverter-month EUR loss.
3. Degradation slopes by inverter and module type.
4. Stacked EUR loss per year per cause.
5. Ticket lead-time case timeline.
6. Held-out nRMSE by inverter.

**Pitch headline numbers:**

- Verified production loss: `EUR 167,715`.
- Verified production energy loss: `1,266,258` kWh.
- Telemetry `DATA_GAP`: `960,570` kWh, about `EUR 126,694`, excluded from the verified-loss claim.
- Largest verified bucket: `DEGRADATION` at `EUR 98,046`.
- Degradation median slope: `-1.08%/year`.
- Faults: `EUR 29,883`.
- Hard outages: `EUR 5,111`.
- Price/operator curtailment: `EUR 18,921`.
- Grid curtailment: `EUR 66`.
- Median held-out twin nRMSE: `4.22%` of kWp.
- Ticket-linked lead examples: `50`; median lead `15.0` days.
- Proof case: `INV 01.01.003` flagged as `FAULT` on `2022-07-03`; ticket opened `2022-07-24`; cumulative identified loss before ticket `EUR 755`.

**Implementation notes:**

- The report script writes both PNG and HTML for every figure.
- Kaleido's launcher in the local venv needed path quoting because the project path contains spaces and a colon. `06_report.py` now applies this small launcher patch automatically before PNG export.

**Verification:**

- All six PNG files were written successfully.
- Pixel standard deviation checks confirm the PNGs are nonblank.
- `pitch/PITCH.md` was generated with the verified-loss, telemetry-gap, and ticket numbers.
