from __future__ import annotations

import re
from typing import Any

import pandas as pd


INVERTER_RE = re.compile(r"(?:INV|WR)\s*(\d{2})\s*[\.\s]\s*(\d{2})\s*[\.\s]\s*(\d{3})")


def parse_inverter_id(value: Any) -> str | None:
    """Normalize text like 'WR 01 .01 .001' to 'INV 01.01.001'."""
    if pd.isna(value):
        return None
    text = str(value)
    match = INVERTER_RE.search(text)
    if not match:
        return None
    return f"INV {match.group(1)}.{match.group(2)}.{match.group(3)}"


def safe_float(value: Any) -> float | None:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

