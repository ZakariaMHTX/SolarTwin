from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load PROJECT_ROOT/.env into os.environ.

    Project-local .env OVERRIDES ambient shell variables: the key checked into
    this project's .env is the one the demo must use, regardless of what the
    developer's shell profile happens to export.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


_load_dotenv()
WORKSPACE_ROOT = PROJECT_ROOT.parent
DATA_ROOT = WORKSPACE_ROOT / "EP-Challenge-Final -"
PLANT_A_ROOT = DATA_ROOT / "Plant A (start here)"
PLANT_B_ROOT = DATA_ROOT / "Plant B  (optional, only plant A is sufficient too)"

OUTPUT_DIR = PROJECT_ROOT / "outputs"
FIGURES_DIR = OUTPUT_DIR / "figures"
DB_PATH = OUTPUT_DIR / "solartwin.duckdb"

MAIN_PARQUET = PLANT_A_ROOT / "1. Main-monitoring-data" / "main_monitoring_data.parquet"
MAIN_XLSB = PLANT_A_ROOT / "1. Main-monitoring-data" / "main_monitoring_data.xlsb"
MAIN_LEGEND_XLSB = PLANT_A_ROOT / "1. Main-monitoring-data" / "main_monitoring_data_legend.xlsb"
SYSTEM_OVERVIEW_XLSX = PLANT_A_ROOT / "2. Additional Data" / "System_Overview.xlsx"
TICKETS_XLSX = PLANT_A_ROOT / "2. Additional Data" / "Tickets.xlsx"
TARIFFS_XLSX = PLANT_A_ROOT / "2. Additional Data" / "feed-in-tarrifs.xlsx"
ERRORCODES_PARQUET = PLANT_A_ROOT / "3. Errorcodes" / "errorcodes.parquet"
ERRORCODES_DESCRIPTION_XLSX = (
    PLANT_A_ROOT / "3. Errorcodes" / "errorcodes description (important).xlsx"
)
GENERAL_INFO_PDF = PLANT_A_ROOT / "(please read first) General information plant A.pdf"
DATA_USE_POLICY = DATA_ROOT / "Data Use Policy.txt"

PLANT_ALTITUDE_COL = "Plant / Altitude (°)"
IRRADIATION_COL = "Plant / Irradiation_average (W/m²)"
AMBIENT_TEMP_COL = "Temperature Sensor / Ambient (°C)"
MODULE_TEMP_COL = "Temperature Sensor / Module (°C)"
DV_COL = "DRD11A / DV (%)"
EVU_COL = "DRD11A / EVU (%)"
JANITZA_P_AC_COL = "Janitza UMG 604 - DRD11A / P_AC_L1..L3 (kW)"
JANITZA_COSPHI_COL = "Janitza UMG 604 - DRD11A / CosPhi_L1..L3"
JANITZA_S_AC_COL = "Janitza UMG 604 - DRD11A / S_AC_L1..L3 (kVA)"

TIMESTAMP_COL = "timestamp"
LOCAL_TIMEZONE = "Europe/Berlin"

DAY_ALTITUDE_DEG = 5.0
MIN_IRRADIATION_W_M2 = 20.0
CURTAILMENT_FREE_PCT = 100.0
TEMP_COEFFICIENT_PER_C = -0.004
RANDOM_SEED = 42

