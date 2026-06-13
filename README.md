# SolarTwin

SolarTwin is a local prototype for the Enerparc dedicated challenge, "Digital Twin of a Solar Plant."

The project builds a plant-level reliability copilot from Enerparc's local Plant A dataset:

1. ingest monitoring, error, ticket, tariff, and inverter metadata into DuckDB;
2. create physics and peer-normalized baselines;
3. train lightweight inverter digital twins;
4. attribute lost energy into operational buckets;
5. convert losses into euros;
6. expose the result through a Streamlit O&M dashboard and an optional OpenAI-powered agent.

Raw data is intentionally not copied into this repository.

## Quickstart

```bash
git clone https://github.com/ZakariaMHTX/SolarTwin.git
cd SolarTwin
uv venv --python python3.12 .venv
uv pip install -r requirements.txt
./.venv/bin/python scripts/run_all.py --through report
./.venv/bin/streamlit run app/dashboard.py --server.port 8521 --server.address 127.0.0.1
```

The Enerparc dataset is restricted and not included; place the `EP-Challenge-Final -` folder next to this repository (paths are configured in `config.py`).

If the shell command `python` resolves to a system Python 2.x, use `./.venv/bin/python` explicitly.

Dashboard URL used in the latest test: `http://127.0.0.1:8521`

## Main Outputs

- `outputs/ledger.csv` — final EUR-denominated loss ledger.
- `outputs/ledger_summary.md` — attribution headline and accounting check.
- `outputs/twin_summary.md` — learned twin metrics and degradation slopes.
- `outputs/figures/` — pitch figures as PNG and HTML.
- `pitch/PITCH.md` — five-minute narrative with filled-in results.
