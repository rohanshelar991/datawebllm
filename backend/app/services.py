from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
from typing import Any

import duckdb
import pandas as pd
import requests
from langchain_groq import ChatGroq

from data_intelligence.analytics import build_dataset_health, infer_suggested_questions, summarize_result
from data_intelligence.constants import MAX_RESULT_ROWS, TABLE_NAME
from data_intelligence.data_utils import github_blob_to_raw, load_dataset, make_safe_columns, normalize_url
from data_intelligence.sql_utils import ensure_limit, enforce_table_whitelist, explain_missing_columns, validate_select_only

from .config import settings
from .db import delete_datasets_by_source_label, get_storage_bucket, load_dataset_bytes, persist_dataset, store_dataset_bytes


def fetch_remote_text(url: str) -> str:
    response = requests.get(github_blob_to_raw(normalize_url(url)), timeout=30)
    response.raise_for_status()
    return response.text


def get_duckdb_connection(df: pd.DataFrame) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(database=":memory:")
    conn.register("df_view", df)
    conn.execute(f"CREATE OR REPLACE TABLE {TABLE_NAME} AS SELECT * FROM df_view")
    return conn


def build_schema_text(conn: duckdb.DuckDBPyConnection) -> str:
    info = conn.execute(f"PRAGMA table_info('{TABLE_NAME}')").df()
    return "\n".join([f"- {row['name']} ({row['type']})" for _, row in info.iterrows()])


def column_catalog(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for col in df.columns:
        non_null = df[col].dropna()
        rows.append(
            {
                "column": col,
                "dtype": str(df[col].dtype),
                "null_pct": round(float(df[col].isna().mean() * 100), 2),
                "distinct_values": int(df[col].nunique(dropna=False)),
                "sample": str(non_null.iloc[0])[:80] if not non_null.empty else "",
            }
        )
    return rows


def build_dataset_summary(dataset_id: str, source_label: str, active_df: pd.DataFrame, schema_text: str) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "source_label": source_label,
        "row_count": int(len(active_df)),
        "column_count": int(len(active_df.columns)),
        "schema_text": schema_text,
        "preview": active_df.head(25).replace({pd.NA: None}).to_dict(orient="records"),
        "health": build_dataset_health(active_df),
        "columns": column_catalog(active_df),
        "suggested_questions": infer_suggested_questions(active_df),
    }


def create_dataset_record_from_upload(file_bytes: bytes, filename: str, source_label: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str], str]:
    file_obj = io.BytesIO(file_bytes)
    file_obj.name = filename
    original_df = load_dataset(file=file_obj)
    active_df, mapping = make_safe_columns(original_df)
    conn = get_duckdb_connection(active_df)
    schema_text = build_schema_text(conn)
    return original_df, active_df, mapping, schema_text


def create_dataset_record_from_url(url: str, source_label: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str], str]:
    remote_text = fetch_remote_text(url)
    original_df = load_dataset(url=url, remote_text=remote_text)
    active_df, mapping = make_safe_columns(original_df)
    conn = get_duckdb_connection(active_df)
    schema_text = build_schema_text(conn)
    return original_df, active_df, mapping, schema_text


def _upload_dataset_bytes(user_id: str, parquet_bytes: bytes) -> str:
    bucket = get_storage_bucket()
    if bucket:
        try:
            blob_name = f"datasets/{user_id}/{uuid.uuid4().hex}.parquet"
            blob = bucket.blob(blob_name)
            blob.upload_from_string(parquet_bytes, content_type="application/octet-stream")
            return f"gs://{bucket.name}/{blob_name}"
        except Exception:
            pass

    return store_dataset_bytes(parquet_bytes)


def _read_dataset_bytes(file_path: str) -> bytes:
    if file_path.startswith("gs://"):
        bucket = get_storage_bucket()
        if not bucket:
            raise ValueError("Firebase Storage bucket is not configured.")
        prefix = f"gs://{bucket.name}/"
        if not file_path.startswith(prefix):
            raise ValueError("Dataset file is stored in an unexpected bucket.")
        blob_name = file_path[len(prefix):]
        return bucket.blob(blob_name).download_as_bytes()
    if file_path.startswith("firestore://"):
        return load_dataset_bytes(file_path)

    with open(file_path, "rb") as handle:
        return handle.read()


def save_dataset_for_user(user_id: str, source_label: str, original_df: pd.DataFrame, active_df: pd.DataFrame, column_mapping: dict[str, str], schema_text: str) -> dict[str, Any]:
    delete_datasets_by_source_label(user_id, source_label)
    parquet_buffer = io.BytesIO()
    active_df.to_parquet(parquet_buffer, index=False)
    dataset_file = _upload_dataset_bytes(user_id, parquet_buffer.getvalue())
    return persist_dataset(
        user_id=user_id,
        source_label=source_label,
        file_path=dataset_file,
        schema_text=schema_text,
        row_count=len(active_df),
        column_count=len(active_df.columns),
        column_mapping=column_mapping,
    )


def load_persisted_dataset(dataset_row: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, str], str]:
    df = pd.read_parquet(io.BytesIO(_read_dataset_bytes(str(dataset_row["file_path"]))))
    return df, dict(dataset_row["column_mapping"]), str(dataset_row["schema_text"])


def generate_sql(question: str, schema_text: str, column_mapping: dict[str, str]) -> str:
    if not settings.groq_api_key:
        raise ValueError("Missing GROQ_API_KEY.")

    mapping_hint = "\n".join([f"- {k} -> {v}" for k, v in list(column_mapping.items())[:40]])
    if len(column_mapping) > 40:
        mapping_hint += "\n- (mapping truncated)"

    prompt = f"""
You are a senior data analyst. Convert the user's question into a SINGLE DuckDB SQL SELECT query.

Rules:
- Use ONLY the table named {TABLE_NAME}.
- Output ONLY SQL.
- Must be a SELECT query.
- Use safe column names from the mapping.
- SQL must be valid DuckDB SQL.
- Include GROUP BY for non-aggregated selected columns.
- For percentages, compute them explicitly on a 0-100 scale.
- Prefer clear aliases and avoid SELECT *.

Schema:
{schema_text}

Original->Safe column mapping:
{mapping_hint}

User question:
{question}
""".strip()

    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=settings.groq_api_key, timeout=45, max_retries=2)
    sql = (llm.invoke(prompt).content or "").strip()
    return re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()


def repair_sql(question: str, schema_text: str, column_mapping: dict[str, str], previous_sql: str, error_message: str) -> str:
    if not settings.groq_api_key:
        raise ValueError("Missing GROQ_API_KEY.")

    mapping_hint = "\n".join([f"- {k} -> {v}" for k, v in list(column_mapping.items())[:40]])
    if len(column_mapping) > 40:
        mapping_hint += "\n- (mapping truncated)"

    prompt = f"""
Fix this DuckDB SQL query.

Rules:
- Output ONLY one DuckDB SELECT query.
- Use ONLY the table named {TABLE_NAME}.
- Keep the query safe and valid.
- Include GROUP BY where needed.

Schema:
{schema_text}

Original->Safe column mapping:
{mapping_hint}

Question:
{question}

Previous SQL:
{previous_sql}

Error:
{error_message}
""".strip()

    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=settings.groq_api_key, timeout=45, max_retries=2)
    sql = (llm.invoke(prompt).content or "").strip()
    return re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()


def execute_question(active_df: pd.DataFrame, schema_text: str, column_mapping: dict[str, str], question: str) -> dict[str, Any]:
    conn = get_duckdb_connection(active_df)
    last_error: Exception | None = None
    sql = ""

    for attempt in range(1, 4):
        try:
            if attempt == 1:
                sql = generate_sql(question, schema_text, column_mapping)
            else:
                sql = repair_sql(question, schema_text, column_mapping, sql, str(last_error))

            parsed = validate_select_only(sql)
            enforce_table_whitelist(parsed, sql)
            sql = ensure_limit(sql, parsed)

            started_at = time.perf_counter()
            result_df = conn.execute(sql).df()
            runtime_ms = (time.perf_counter() - started_at) * 1000.0
            answer = summarize_result(result_df)
            explanation = (
                f"Executed a validated DuckDB query against `{TABLE_NAME}` and answered only from the returned rows. "
                f"Rows returned: {len(result_df):,}. Query runtime: {runtime_ms:.1f} ms."
            )
            return {
                "question": question,
                "answer": answer,
                "sql": sql,
                "explanation": explanation,
                "attempts": attempt,
                "runtime_ms": round(runtime_ms, 2),
                "rows_returned": int(len(result_df)),
                "result": result_df.head(MAX_RESULT_ROWS).replace({pd.NA: None}).to_dict(orient="records"),
            }
        except Exception as exc:  # pragma: no cover - exercised through API behavior
            last_error = exc

    available_cols = list(active_df.columns)
    msg = str(last_error)
    missing_cols_expl = explain_missing_columns(msg, available_cols)
    if missing_cols_expl:
        raise ValueError(missing_cols_expl) from last_error
    raise ValueError(msg) from last_error
