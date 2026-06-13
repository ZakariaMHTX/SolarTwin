from __future__ import annotations

import os
import re
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "outputs" / "solartwin.duckdb"

SCHEMA_NOTES = {
    "ledger": "Final monthly loss ledger by inverter, cause bucket, kWh, EUR, tariff, and ticket validation flag.",
    "ledger_by_year_cause": "Yearly plant-level loss rollup by cause bucket.",
    "ledger_top20_inverter_cause": "Top inverter/cause pairs by total EUR loss.",
    "ledger_module_type_year": "Loss rollup by year, module type, and cause bucket.",
    "ticket_lead_events": "Daily events where a model flag preceded a later service ticket.",
    "twin_model_metrics": "Held-out digital-twin accuracy metrics per inverter.",
    "degradation_slopes": "Theil-Sen degradation slope per inverter.",
    "degradation_module_type": "Module-type aggregation of degradation slopes.",
    "loss_intervals": "Interval-level classified shortfalls. Large table; aggregate before displaying.",
    "error_catalog": "Error-code base descriptions and severity classes.",
}

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|copy|attach|detach|pragma|vacuum|call)\b",
    re.IGNORECASE,
)


@dataclass
class AgentResult:
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    used_llm: bool = False


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def get_schema(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_name
            """
        ).fetchall()
        parts: list[str] = []
        for (table,) in rows:
            note = SCHEMA_NOTES.get(table, "")
            cols = con.execute(f"DESCRIBE {table}").fetchdf()
            col_text = ", ".join(cols["column_name"].astype(str).tolist())
            parts.append(f"{table}: {note} Columns: {col_text}")
        return "\n".join(parts)


def run_sql(query: str, db_path: Path | str = DEFAULT_DB_PATH, max_rows: int = 200) -> pd.DataFrame:
    cleaned = query.strip().rstrip(";")
    if not cleaned.lower().startswith(("select", "with")):
        raise ValueError("Only SELECT/WITH queries are allowed.")
    if FORBIDDEN_SQL.search(cleaned):
        raise ValueError("Query contains a forbidden write/admin keyword.")
    limited = f"SELECT * FROM ({cleaned}) AS q LIMIT {int(max_rows)}"
    with connect(db_path) as con:
        return con.execute(limited).fetchdf()


def lookup_error_code(code: int, db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    base_code = int(code) % 65536
    return run_sql(
        f"""
        SELECT code, description, severity_class
        FROM error_catalog
        WHERE code = {base_code}
        """,
        db_path=db_path,
        max_rows=10,
    )


def _format_money(value: float | int | None) -> str:
    if value is None:
        return "EUR 0"
    return f"EUR {float(value):,.0f}"


def _format_kwh(value: float | int | None) -> str:
    if value is None:
        return "0 kWh"
    return f"{float(value):,.0f} kWh"


def _template_service_priority(db_path: Path | str) -> AgentResult:
    # Service priority ranks only ACTIONABLE losses: faults, outages, and local
    # underperformance. Curtailment is not serviceable, degradation is not repairable
    # by a truck roll, and DATA_GAP is a monitoring issue, not a hardware issue.
    sql = """
    SELECT
      inverter,
      sum(lost_eur) AS lost_eur,
      sum(lost_kwh) AS lost_kwh,
      string_agg(DISTINCT bucket, ', ' ORDER BY bucket) AS buckets,
      bool_or(validated_by_ticket) AS has_ticket_validation
    FROM ledger
    WHERE bucket IN ('FAULT', 'OUTAGE', 'UNDERPERFORMANCE_LOCAL')
    GROUP BY inverter
    ORDER BY lost_eur DESC
    LIMIT 10
    """
    df = run_sql(sql, db_path)
    top = df.iloc[0].to_dict()
    answer = (
        f"Service priority is {top['inverter']}: it has the highest actionable loss "
        f"({_format_money(top['lost_eur'])}, {_format_kwh(top['lost_kwh'])}) "
        f"across serviceable causes ({top['buckets']}); curtailment, degradation and "
        "telemetry gaps are excluded from this ranking. "
        "Use the inverter drill-down tab to inspect its worst month and time series."
    )
    return AgentResult(answer=answer, sql=sql, rows=df.to_dict(orient="records"))


def _template_curtailment_2023(db_path: Path | str) -> AgentResult:
    sql = """
    SELECT
      bucket,
      sum(lost_kwh) AS lost_kwh,
      sum(lost_eur) AS lost_eur,
      sum(n_intervals) AS n_intervals
    FROM ledger
    WHERE year = 2023
      AND bucket IN ('CURTAILMENT_GRID', 'CURTAILMENT_PRICE')
    GROUP BY bucket
    ORDER BY lost_eur DESC
    """
    df = run_sql(sql, db_path)
    # Always report BOTH buckets explicitly: an absent row means EUR 0, and saying
    # so matters (grid curtailment is potentially compensable; price curtailment is not).
    found = {row.bucket: row for row in df.itertuples()}
    fragments = []
    for bucket, label in [
        ("CURTAILMENT_GRID", "grid-ordered curtailment (EVU)"),
        ("CURTAILMENT_PRICE", "operator/price curtailment (DV)"),
    ]:
        row = found.get(bucket)
        if row is not None:
            fragments.append(f"{label}: {_format_money(row.lost_eur)} / {_format_kwh(row.lost_kwh)}")
        else:
            fragments.append(f"{label}: EUR 0 (no intervals in the 2023 ledger)")
    answer = "For 2023, the ledger shows " + "; ".join(fragments) + "."
    return AgentResult(answer=answer, sql=sql, rows=df.to_dict(orient="records"))


def _template_worst_month(question: str, db_path: Path | str) -> AgentResult:
    match = re.search(r"INV\s+\d{2}\.\d{2}\.\d{3}", question, re.IGNORECASE)
    inverter = match.group(0).upper() if match else None
    if not inverter:
        top = run_sql(
            """
            SELECT inverter, sum(lost_eur) AS lost_eur
            FROM ledger
            GROUP BY inverter
            ORDER BY lost_eur DESC
            LIMIT 1
            """,
            db_path,
        )
        inverter = str(top.iloc[0]["inverter"])

    # Rank verified production-loss months; report telemetry gaps separately so a
    # monitoring blackout is never presented as the inverter's "worst month".
    sql = f"""
    SELECT
      inverter,
      month,
      bucket,
      sum(lost_kwh) AS lost_kwh,
      sum(lost_eur) AS lost_eur,
      bool_or(validated_by_ticket) AS validated_by_ticket
    FROM ledger
    WHERE inverter = '{inverter}'
      AND bucket != 'DATA_GAP'
    GROUP BY 1, 2, 3
    ORDER BY lost_eur DESC
    LIMIT 10
    """
    df = run_sql(sql, db_path)
    gap = run_sql(
        f"""
        SELECT COALESCE(sum(lost_kwh), 0) AS gap_kwh
        FROM ledger
        WHERE inverter = '{inverter}' AND bucket = 'DATA_GAP'
        """,
        db_path,
    )
    gap_kwh = float(gap.iloc[0]["gap_kwh"]) if not gap.empty else 0.0
    if df.empty:
        answer = f"I did not find verified loss rows for {inverter}."
    else:
        top = df.iloc[0].to_dict()
        answer = (
            f"{inverter}'s worst verified-loss month is {str(top['month'])[:10]}, "
            f"bucket {top['bucket']}, with {_format_money(top['lost_eur'])} "
            f"({_format_kwh(top['lost_kwh'])}). "
            f"Ticket validation for that row: {bool(top['validated_by_ticket'])}."
        )
    if gap_kwh > 0:
        answer += (
            f" Separately, {_format_kwh(gap_kwh)} of this inverter's expected energy "
            "falls in telemetry DATA_GAP periods (monitoring availability issue, "
            "not claimed as production loss)."
        )
    return AgentResult(answer=answer, sql=sql, rows=df.to_dict(orient="records"))


def _template_summary(db_path: Path | str) -> AgentResult:
    sql = """
    SELECT
      bucket,
      sum(lost_kwh) AS lost_kwh,
      sum(lost_eur) AS lost_eur,
      sum(n_intervals) AS n_intervals
    FROM ledger
    GROUP BY bucket
    ORDER BY lost_eur DESC
    """
    df = run_sql(sql, db_path)
    verified = df[df["bucket"] != "DATA_GAP"] if not df.empty else df
    total = float(verified["lost_eur"].sum()) if not verified.empty else 0.0
    gap_kwh = float(df.loc[df["bucket"] == "DATA_GAP", "lost_kwh"].sum()) if not df.empty else 0.0
    answer = (
        f"Total verified production loss is {_format_money(total)} "
        f"(plus {_format_kwh(gap_kwh)} unaccounted in telemetry DATA_GAP periods, "
        "not claimed as loss). "
        "The largest verified buckets are "
        + ", ".join(f"{r.bucket} ({_format_money(r.lost_eur)})" for r in verified.head(3).itertuples())
        + "."
    )
    return AgentResult(answer=answer, sql=sql, rows=df.to_dict(orient="records"))


def answer_with_templates(question: str, db_path: Path | str = DEFAULT_DB_PATH) -> AgentResult:
    q = question.lower()
    if any(term in q for term in ["service first", "service priority", "which inverter", "worst inverter"]):
        return _template_service_priority(db_path)
    if "curtail" in q and "2023" in q:
        return _template_curtailment_2023(db_path)
    if "worst month" in q or re.search(r"INV\s+\d{2}\.\d{2}\.\d{3}", question, re.IGNORECASE):
        return _template_worst_month(question, db_path)
    return _template_summary(db_path)


def _can_answer_with_template(question: str) -> bool:
    q = question.lower()
    return bool(
        any(term in q for term in ["service first", "service priority", "which inverter", "worst inverter"])
        or ("curtail" in q and "2023" in q)
        or "worst month" in q
        or re.search(r"INV\s+\d{2}\.\d{2}\.\d{3}", question, re.IGNORECASE)
        or any(term in q for term in ["summary", "overview", "total loss", "verified loss", "largest bucket"])
    )


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _resolve_llm() -> tuple[str | None, str | None, str | None]:
    """Return (api_key, base_url, model) for the first configured provider.

    Provider order: OpenAI > Google AI Studio (Gemini) > NVIDIA NIM.
    OpenAI is preferred for the live hackathon demo because it responds much
    faster on the grounded-answer path than MiniMax M3 in this app.
    """
    if os.getenv("OPENAI_API_KEY"):
        return os.getenv("OPENAI_API_KEY"), None, os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # Keep Gemini ahead of NVIDIA as the second choice because MiniMax can be
    # slow in the chat-completions tool loop even when the key is valid.
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key:
        return gemini_key, GEMINI_BASE_URL, os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    nvidia_key = os.getenv("NVIDIA_API_KEY")
    if nvidia_key:
        return nvidia_key, NVIDIA_BASE_URL, os.getenv("NVIDIA_MODEL", "minimaxai/minimax-m3")
    return None, None, None


def _polish_sql_backed_answer(client: Any, model: str, question: str, fallback: AgentResult) -> AgentResult | None:
    """Use the LLM for prose only after DuckDB/templates have fixed the facts."""
    payload = {
        "question": question,
        "draft_answer": fallback.answer,
        "sql": fallback.sql,
        "top_rows": (fallback.rows or [])[:3],
    }
    response = client.with_options(timeout=25.0, max_retries=0).chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are SolarTwin. Rewrite the SQL-backed draft into a concise "
                    "answer for a solar O&M dashboard. Use ONLY the provided draft "
                    "and rows. Do not add new numbers. Keep DATA_GAP separate from "
                    "verified production loss. Maximum 5 short bullets."
                ),
            },
            {"role": "user", "content": json.dumps(payload, default=str)},
        ],
        temperature=0.1,
        max_tokens=900,
    )
    if not response.choices:
        return None
    content = (response.choices[0].message.content or "").strip()
    if not content:
        return None
    return AgentResult(answer=content, sql=fallback.sql, rows=fallback.rows, used_llm=True)


def answer_question(question: str, db_path: Path | str = DEFAULT_DB_PATH) -> AgentResult:
    # The deterministic templates keep the live demo useful without any API key.
    api_key, base_url, model = _resolve_llm()
    if not api_key:
        return answer_with_templates(question, db_path)

    try:
        from openai import OpenAI
    except Exception:
        return answer_with_templates(question, db_path)

    # Hard per-request timeout: a hung provider must degrade to templates,
    # never freeze the dashboard on a spinner.
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=45.0, max_retries=1)

    # Common demo questions have deterministic SQL templates. Answer those from
    # DuckDB first, then use the model only to phrase the already-grounded facts;
    # this avoids slow tool-calling on providers that support chat but struggle
    # to finish function-call loops.
    if _can_answer_with_template(question):
        fallback = answer_with_templates(question, db_path)
        try:
            polished = _polish_sql_backed_answer(client, model, question, fallback)
            if polished is not None:
                return polished
        except Exception:
            pass
        return fallback

    system = (
        "You are SolarTwin, a concise solar O&M analyst. "
        "Answer with numbers from DuckDB only. Cite the table/filter used for every number. "
        "Prefer ledger, ledger_by_year_cause, ticket_lead_events, degradation_slopes, and twin_model_metrics. "
        "Use tools before answering. Do not invent raw data. "
        "IMPORTANT semantics: the bucket DATA_GAP is NOT a production loss — it is energy "
        "unaccounted while inverter telemetry was missing but the plant meter shows the site "
        "producing (a monitoring-availability finding). Exclude DATA_GAP from loss rankings and "
        "totals unless the user explicitly asks about telemetry/data gaps; if it is relevant, "
        "report it separately and label it as such. Only FAULT, OUTAGE and UNDERPERFORMANCE_LOCAL "
        "are serviceable causes; DEGRADATION and CURTAILMENT_* are not fixable by maintenance. "
        "Amounts are in EUR; energy in kWh; data covers 2017-2026 for 65 inverters."
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_schema",
                "description": "Return available DuckDB tables, columns, and table semantics.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": "Run a read-only SELECT/WITH SQL query against the SolarTwin DuckDB. Results are limited to 200 rows.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_error_code",
                "description": "Look up an inverter error-code description. Raw codes are automatically reduced to the base code.",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "integer"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    sql_used: str | None = None
    rows_used: list[dict[str, Any]] | None = None
    deadline = time.monotonic() + 100.0  # total wall-clock budget for the loop
    try:
        for _ in range(6):
            if time.monotonic() > deadline:
                break
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                # NVIDIA NIM returns an EMPTY choices array when max_tokens is
                # omitted (observed with minimaxai/minimax-m3). Generous budget:
                # MiniMax M3 is a reasoning model and spends tokens thinking.
                max_tokens=4096,
            )
            if not response.choices:
                break
            message = response.choices[0].message
            if not message.tool_calls:
                # Some models occasionally return an empty final message after
                # tool calls — treat that as a failure, not as an answer.
                if not (message.content or "").strip():
                    break
                return AgentResult(
                    answer=message.content,
                    sql=sql_used,
                    rows=rows_used,
                    used_llm=True,
                )

            messages.append(message.model_dump(exclude_none=True))
            for tool_call in message.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments or "{}")
                # Tool failures (bad column name, SQL typo) are returned TO the
                # model as the tool result so it can read the error and retry —
                # they must never abort the conversation.
                try:
                    if name == "get_schema":
                        result: Any = get_schema(db_path)
                    elif name == "run_sql":
                        sql_used = args["query"]
                        df = run_sql(sql_used, db_path)
                        rows_used = df.to_dict(orient="records")
                        result = rows_used
                    elif name == "lookup_error_code":
                        df = lookup_error_code(int(args["code"]), db_path)
                        result = df.to_dict(orient="records")
                    else:
                        result = {"error": f"Unknown tool {name}"}
                except Exception as tool_exc:
                    result = {
                        "error": f"{type(tool_exc).__name__}: {tool_exc}",
                        "hint": "Check get_schema for valid table and column names, then retry.",
                    }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        fallback = answer_with_templates(question, db_path)
        try:
            polished = _polish_sql_backed_answer(client, model, question, fallback)
            if polished is not None:
                return polished
        except Exception:
            pass
        fallback.answer = fallback.answer + "\n\n_Answered from the deterministic ledger templates._"
        return fallback
    except Exception as exc:
        fallback = answer_with_templates(question, db_path)
        fallback.answer = f"{fallback.answer}\n\n_LLM unavailable ({type(exc).__name__}); answered from the deterministic ledger templates instead._"
        return fallback
