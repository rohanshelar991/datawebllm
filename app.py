import io
import json
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from sqlglot import exp, parse_one

from data_intelligence.security import is_session_expired, parse_timeout_minutes, verify_password

try:
    import altair as alt
except Exception:  # pragma: no cover
    alt = None  # type: ignore[assignment]


TABLE_NAME = "data_table"
MAX_RESULT_ROWS = 500
DEFAULT_SAMPLE_URL = "https://github.com/Geo-y20/Telco-Customer-Churn-/blob/main/Telco%20Customer%20Churn.csv"


_DOTENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
# If the environment variable exists but is empty, prefer the local `.env`.
if not (os.getenv("GROQ_API_KEY") or "").strip():
    load_dotenv(dotenv_path=_DOTENV_PATH, override=True)
else:
    load_dotenv(dotenv_path=_DOTENV_PATH, override=False)

st.set_page_config(page_title="Conversational Data Intelligence", layout="wide")


@st.cache_resource
def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(database=":memory:")


@st.cache_data(show_spinner=False)
def fetch_remote_text(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def get_groq_api_key() -> str:
    # Priority:
    # 1) Environment variable (best for deployment)
    # 2) Streamlit secrets (Streamlit Community Cloud / managed deploy)
    # 3) Local `.env` file (dev convenience; ignored by git)
    # 4) Session-only key (manual fallback)
    env_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if env_key:
        return env_key

    try:
        secrets_key = str(st.secrets.get("GROQ_API_KEY", "")).strip()
        if secrets_key:
            return secrets_key
    except Exception:
        pass

    # Fall back to `.env` directly (in case the process env had GROQ_API_KEY="" which prevented dotenv load).
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(_DOTENV_PATH)
        file_key = str(vals.get("GROQ_API_KEY", "") or "").strip()
        if file_key and file_key != "replace_me":
            return file_key
    except Exception:
        pass

    return str(st.session_state.get("session_groq_key", "") or "").strip()


def get_app_password() -> str:
    env_password = (os.getenv("APP_PASSWORD") or "").strip()
    if env_password:
        return env_password
    try:
        return str(st.secrets.get("APP_PASSWORD", "")).strip()
    except Exception:
        return ""


def get_auth_timeout_minutes() -> int:
    env_timeout = (os.getenv("SESSION_TIMEOUT_MINUTES") or "").strip()
    if env_timeout:
        return parse_timeout_minutes(env_timeout, default=30)
    try:
        return parse_timeout_minutes(str(st.secrets.get("SESSION_TIMEOUT_MINUTES", "")).strip(), default=30)
    except Exception:
        return 30


def enforce_authentication() -> None:
    expected_password = get_app_password()
    if not expected_password:
        return

    timeout_minutes = get_auth_timeout_minutes()
    now_ts = time.time()
    is_authenticated = bool(st.session_state.get("is_authenticated"))
    last_active_at = st.session_state.get("auth_last_active_at")

    if is_authenticated and is_session_expired(last_active_at, timeout_minutes, now_ts):
        st.session_state.is_authenticated = False
        st.session_state.auth_error = "Your session expired. Please sign in again."
        is_authenticated = False

    if is_authenticated:
        st.session_state.auth_last_active_at = now_ts
        return

    st.markdown("## Secure Access")
    st.caption("This deployment is protected. Enter the app password to continue.")
    auth_error = str(st.session_state.get("auth_error", "") or "").strip()
    if auth_error:
        st.error(auth_error)

    with st.form("auth_login_form", clear_on_submit=True):
        entered_password = st.text_input("App password", type="password", placeholder="Enter deployment password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if verify_password(entered_password, expected_password):
            st.session_state.is_authenticated = True
            st.session_state.auth_last_active_at = now_ts
            st.session_state.auth_error = ""
            st.rerun()
        st.session_state.auth_error = "Invalid password."

    st.stop()


def _github_blob_to_raw(url: str) -> str:
    url = normalize_url(url)
    # Example:
    # https://github.com/<org>/<repo>/blob/<branch>/path.csv
    # -> https://raw.githubusercontent.com/<org>/<repo>/<branch>/path.csv
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$", url.strip())
    if not m:
        return url.strip()
    owner, repo, branch, path = m.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


def normalize_url(url: str) -> str:
    # Users often paste URLs with spaces/line-breaks (e.g., "https:/ /github.com/.../Telco%20Customer%2 0Churn.csv").
    # Remove whitespace and fix common single-slash scheme typos.
    u = (url or "").strip()
    u = re.sub(r"\s+", "", u)
    if u.startswith("https:/") and not u.startswith("https://"):
        u = "https://" + u[len("https:/") :]
    if u.startswith("http:/") and not u.startswith("http://"):
        u = "http://" + u[len("http:/") :]
    return u


def _make_safe_identifier(name: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip()).strip("_").lower()
    if not safe:
        safe = "col"
    if safe[0].isdigit():
        safe = f"c_{safe}"
    return safe


def make_safe_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    original = list(df.columns)
    mapping: Dict[str, str] = {}
    used: Dict[str, int] = {}

    new_cols = []
    for col in original:
        base = _make_safe_identifier(str(col))
        count = used.get(base, 0)
        used[base] = count + 1
        safe = base if count == 0 else f"{base}_{count+1}"
        mapping[str(col)] = safe
        new_cols.append(safe)

    df2 = df.copy()
    df2.columns = new_cols
    return df2, mapping


def load_dataset(file: Any = None, url: Optional[str] = None) -> pd.DataFrame:
    if url and url.strip():
        cleaned = normalize_url(url)
        raw_url = _github_blob_to_raw(cleaned)
        payload = fetch_remote_text(raw_url)
        # Try CSV first (most common for this assignment)
        try:
            return pd.read_csv(io.StringIO(payload))
        except Exception:
            return pd.read_json(io.StringIO(payload))

    if file is None:
        raise ValueError("No dataset provided")

    name = getattr(file, "name", "") or ""
    if name.lower().endswith(".csv"):
        return pd.read_csv(file)
    if name.lower().endswith(".xlsx") or name.lower().endswith(".xls"):
        return pd.read_excel(file)
    if name.lower().endswith(".json"):
        return pd.read_json(file)
    raise ValueError("Unsupported file type. Use CSV, XLSX, or JSON.")


def register_dataframe(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    conn.register("df_view", df)
    conn.execute(f"CREATE OR REPLACE TABLE {TABLE_NAME} AS SELECT * FROM df_view")


def activate_dataset(df: pd.DataFrame, mapping: Dict[str, str], source_label: str) -> None:
    conn = get_duckdb_connection()
    register_dataframe(conn, df)
    st.session_state.dataset_loaded = True
    st.session_state.column_mapping = mapping
    st.session_state.schema_text = build_schema_text(conn)
    st.session_state.active_df = df
    st.session_state.active_source = source_label


def build_schema_text(conn: duckdb.DuckDBPyConnection) -> str:
    # DuckDB PRAGMA table_info provides stable schema info.
    info = conn.execute(f"PRAGMA table_info('{TABLE_NAME}')").df()
    lines = []
    for _, row in info.iterrows():
        lines.append(f"- {row['name']} ({row['type']})")
    return "\n".join(lines)


def list_columns(conn: duckdb.DuckDBPyConnection) -> list[str]:
    info = conn.execute(f"PRAGMA table_info('{TABLE_NAME}')").df()
    return [str(x) for x in info["name"].tolist()]


def inverse_mapping(mapping: Dict[str, str]) -> Dict[str, str]:
    inv: Dict[str, str] = {}
    for original, safe in mapping.items():
        inv[safe] = original
    return inv


def column_type_map(conn: duckdb.DuckDBPyConnection) -> Dict[str, str]:
    info = conn.execute(f"PRAGMA table_info('{TABLE_NAME}')").df()
    out: Dict[str, str] = {}
    for _, row in info.iterrows():
        out[str(row["name"])] = str(row["type"])
    return out


def is_numeric_dtype(dtype: Any) -> bool:
    return pd.api.types.is_numeric_dtype(dtype)


def is_datetime_dtype(dtype: Any) -> bool:
    return pd.api.types.is_datetime64_any_dtype(dtype)


def build_dataset_health(df: pd.DataFrame) -> Dict[str, Any]:
    row_count = int(len(df))
    col_count = int(len(df.columns))
    total_cells = max(row_count * max(col_count, 1), 1)
    null_cells = int(df.isna().sum().sum())
    duplicate_rows = int(df.duplicated().sum()) if row_count else 0
    numeric_cols = [c for c in df.columns if is_numeric_dtype(df[c])]
    datetime_cols = [c for c in df.columns if is_datetime_dtype(df[c])]
    categorical_cols = [c for c in df.columns if c not in numeric_cols and c not in datetime_cols]
    completeness_pct = round(100.0 * (1.0 - (null_cells / total_cells)), 2)
    duplicate_pct = round(100.0 * duplicate_rows / max(row_count, 1), 2)
    memory_mb = round(float(df.memory_usage(deep=True).sum()) / (1024 * 1024), 2)
    columns_with_nulls = int((df.isna().sum() > 0).sum())
    high_null_cols = int((df.isna().mean() >= 0.3).sum()) if row_count else 0
    unique_ratio = float(df.nunique(dropna=False).sum()) / total_cells
    quality_score = max(
        0,
        round(
            100
            - min(40.0, 100.0 * null_cells / total_cells)
            - min(25.0, duplicate_pct * 2.5)
            - min(15.0, high_null_cols * 2.5),
            1,
        ),
    )
    issues: List[str] = []
    if duplicate_rows:
        issues.append(f"{duplicate_rows:,} duplicate rows detected.")
    if high_null_cols:
        issues.append(f"{high_null_cols:,} columns have at least 30% missing values.")
    if not issues:
        issues.append("No major structural quality issues detected.")

    return {
        "rows": row_count,
        "columns": col_count,
        "null_cells": null_cells,
        "columns_with_nulls": columns_with_nulls,
        "duplicate_rows": duplicate_rows,
        "completeness_pct": completeness_pct,
        "duplicate_pct": duplicate_pct,
        "memory_mb": memory_mb,
        "numeric_cols": len(numeric_cols),
        "categorical_cols": len(categorical_cols),
        "datetime_cols": len(datetime_cols),
        "quality_score": quality_score,
        "issues": issues,
        "unique_ratio": round(unique_ratio, 3),
    }


def infer_suggested_questions(df: pd.DataFrame) -> List[str]:
    cols = list(df.columns)
    lower = {c: c.lower() for c in cols}
    numeric_cols = [c for c in cols if is_numeric_dtype(df[c])]
    categorical_cols = [c for c in cols if not is_numeric_dtype(df[c]) and not is_datetime_dtype(df[c])]
    datetime_cols = [c for c in cols if is_datetime_dtype(df[c])]
    suggestions: List[str] = []

    churn_col = next((c for c in cols if "churn" in lower[c] or "status" in lower[c]), None)
    customer_col = next((c for c in cols if "customer" in lower[c] or "id" in lower[c]), None)
    segment_col = next((c for c in cols if any(k in lower[c] for k in ["gender", "contract", "plan", "segment", "category", "region"])), None)
    amount_col = next((c for c in numeric_cols if any(k in lower[c] for k in ["charge", "sales", "revenue", "amount", "price"])), None)
    tenure_col = next((c for c in numeric_cols if any(k in lower[c] for k in ["tenure", "age", "months", "days", "score"])), None)
    time_col = next((c for c in datetime_cols if any(k in lower[c] for k in ["date", "month", "time", "created"])), None)

    if churn_col and segment_col:
        suggestions.append(f"What is the churn or status rate by {segment_col}?")
    if amount_col and segment_col:
        suggestions.append(f"Show total and average {amount_col} by {segment_col}.")
    if tenure_col and churn_col:
        suggestions.append(f"What is the average {tenure_col} by {churn_col}?")
    if amount_col:
        suggestions.append(f"Who are the top 10 records by {amount_col}?")
    if customer_col:
        suggestions.append(f"How many distinct {customer_col} values are in the dataset?")
    if time_col and amount_col:
        suggestions.append(f"Show the trend of {amount_col} over {time_col}.")
    if segment_col:
        suggestions.append(f"What are the top categories in {segment_col}?")
    if numeric_cols:
        suggestions.append(f"Which records are outliers in {numeric_cols[0]}?")

    fallback = [
        "What are the top 10 segments in this dataset?",
        "Which columns seem most useful for executive reporting?",
        "Summarize the most important trends visible in this dataset.",
    ]
    for item in fallback:
        if len(suggestions) >= 8:
            break
        suggestions.append(item)
    deduped: List[str] = []
    for item in suggestions:
        if item not in deduped:
            deduped.append(item)
    return deduped[:8]


def build_query_package(entry: Dict[str, Any]) -> bytes:
    result_df = entry.get("df", pd.DataFrame())
    meta = {
        "question": entry.get("question", ""),
        "answer": entry.get("content", ""),
        "sql": entry.get("sql", ""),
        "explanation": entry.get("explanation", ""),
        "meta": entry.get("meta", ""),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(meta, indent=2))
        zf.writestr("query.sql", str(entry.get("sql", "") or ""))
        if isinstance(result_df, pd.DataFrame) and not result_df.empty:
            zf.writestr("result.csv", result_df.to_csv(index=False))
    buffer.seek(0)
    return buffer.getvalue()


def render_result_insights(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        st.caption("No result insights available.")
        return

    numeric_cols = [c for c in df.columns if is_numeric_dtype(df[c])]
    cols = st.columns(min(4, max(len(numeric_cols), 1)))
    cols[0].metric("Returned rows", f"{len(df):,}")
    if numeric_cols:
        first = numeric_cols[0]
        cols[min(1, len(cols) - 1)].metric(f"Sum of {first}", f"{df[first].sum():,.2f}")
        if len(cols) > 2:
            cols[2].metric(f"Avg of {first}", f"{df[first].mean():,.2f}")
        if len(cols) > 3:
            cols[3].metric(f"Max of {first}", f"{df[first].max():,.2f}")
    else:
        distinct_hint = df.nunique(dropna=False).sum()
        cols[min(1, len(cols) - 1)].metric("Distinct values", f"{int(distinct_hint):,}")

    insights: List[str] = []
    if numeric_cols:
        top_metric = numeric_cols[0]
        top_row = df.sort_values(top_metric, ascending=False).head(1)
        if not top_row.empty:
            label_col = df.columns[0]
            insights.append(f"Top row by `{top_metric}` is `{top_row.iloc[0][label_col]}` with value `{top_row.iloc[0][top_metric]}`.")
    if len(df.columns) >= 2 and not numeric_cols:
        insights.append("This result is mostly categorical, so the table and chart are likely more informative than scalar KPIs.")
    if insights:
        st.caption(" | ".join(insights))


def build_filter_mask(df: pd.DataFrame, filters: List[Dict[str, Any]]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for item in filters:
        col = item["column"]
        if col not in df.columns:
            continue
        series = df[col]
        if item["kind"] == "numeric_range":
            low, high = item["value"]
            mask &= series.fillna(low).between(low, high)
        elif item["kind"] == "categorical_values":
            values = item["value"]
            if values:
                mask &= series.astype(str).isin([str(v) for v in values])
        elif item["kind"] == "text_contains":
            term = str(item["value"]).strip()
            if term:
                mask &= series.astype(str).str.contains(term, case=False, na=False)
    return mask


def analyze_column(conn: duckdb.DuckDBPyConnection, col: str, col_type: str) -> Dict[str, Any]:
    nulls = conn.execute(f"SELECT SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) FROM {TABLE_NAME}").fetchone()[0]
    result: Dict[str, Any] = {"nulls": int(nulls or 0)}

    t = col_type.upper()
    is_numeric = any(x in t for x in ["INT", "DECIMAL", "DOUBLE", "REAL", "FLOAT", "HUGEINT", "UBIGINT", "NUMERIC"])
    if is_numeric:
        row = conn.execute(
            f"""
            SELECT
              MIN({col}) AS min,
              MAX({col}) AS max,
              AVG({col}) AS avg
            FROM {TABLE_NAME}
            """.strip()
        ).fetchone()
        result.update({"min": row[0], "max": row[1], "avg": row[2]})
    else:
        top = conn.execute(
            f"""
            SELECT {col} AS value, COUNT(*) AS count
            FROM {TABLE_NAME}
            GROUP BY {col}
            ORDER BY count DESC
            LIMIT 10
            """.strip()
        ).df()
        result["top_values"] = top

    return result


def _validate_select_only(sql: str) -> exp.Expression:
    parsed = parse_one(sql, read="duckdb")
    illegal = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Attach, exp.Detach)
    # sqlglot Expression.find expects expression classes, not a predicate.
    if parsed.find(*illegal) is not None:
        raise ValueError("Only SELECT queries are allowed.")

    # Allow SELECT / UNION / WITH ... SELECT
    has_select = parsed.find(exp.Select)
    if not has_select:
        raise ValueError("Only SELECT queries are allowed.")

    # Block multiple statements
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("Multiple statements are not allowed.")

    return parsed


def _enforce_table_whitelist(parsed: exp.Expression, sql: str) -> None:
    table_names = {t.name.lower() for t in parsed.find_all(exp.Table)}
    if not table_names:
        raise ValueError("Query must reference the dataset table.")
    allowed = {TABLE_NAME.lower()}
    if not table_names.issubset(allowed):
        raise ValueError(f"Query can only reference table '{TABLE_NAME}'. Found: {sorted(table_names)}")

    # Block reading from system tables / functions (best-effort)
    if "sqlite_master" in sql.lower() or "information_schema" in sql.lower():
        raise ValueError("System tables are not allowed.")


def _explain_missing_columns(error_message: str, available: list[str]) -> Optional[str]:
    # DuckDB typically reports: Binder Error: Referenced column "foo" not found in FROM clause!
    m = re.search(r'Referenced column "([^"]+)" not found', error_message)
    if not m:
        return None
    missing = m.group(1)
    preview = ", ".join(available[:30])
    suffix = "" if len(available) <= 30 else ", …"
    return f'Missing column `{missing}`. Available columns: {preview}{suffix}'


def _ensure_limit(sql: str, parsed: exp.Expression) -> str:
    select = parsed.find(exp.Select)
    if isinstance(select, exp.Select) and select.args.get("limit") is None:
        return sql.rstrip().rstrip(";") + f" LIMIT {MAX_RESULT_ROWS}"
    return sql


def generate_sql(question: str, schema_text: str, column_mapping: Dict[str, str]) -> str:
    api_key = get_groq_api_key()
    if not api_key.strip():
        raise ValueError("Missing GROQ_API_KEY (required to generate SQL from natural language).")

    mapping_hint = "\n".join([f"- {k} -> {v}" for k, v in list(column_mapping.items())[:40]])
    if len(column_mapping) > 40:
        mapping_hint += "\n- (mapping truncated)"

    prompt = f"""
You are a data analyst. Convert the user's question into a SINGLE DuckDB SQL SELECT query.

Rules (must follow):
- Use ONLY the table named {TABLE_NAME}.
- Output ONLY SQL. No markdown, no explanation.
- Must be a SELECT query (no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/ATTACH).
- Prefer explicit columns instead of SELECT *.
- Use the SAFE column names from the mapping below. The user may mention original names, but you must use safe names.
- SQL must be valid DuckDB SQL.
- If selecting any non-aggregated column, include it in GROUP BY.
- Avoid UNION unless strictly needed; prefer GROUP BY + conditional aggregation.
- If the question asks for a rate/percentage, compute it (e.g., 100.0 * yes_count / total_count) and alias it clearly.
- When asked for a churn rate, do NOT return only counts. Return both total count and churn_rate_pct.
- Use conditional aggregation with FILTER or CASE WHEN, e.g.:
  - COUNT(*) FILTER (WHERE churn='Yes') AS churn_yes
  - 100.0 * COUNT(*) FILTER (WHERE churn='Yes') / COUNT(*) AS churn_rate_pct
- If the question is ambiguous, pick the simplest reasonable interpretation using available columns.

Schema:
{schema_text}

Original->Safe column mapping (use safe names in SQL):
{mapping_hint}

User question:
{question}
""".strip()

    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=api_key, timeout=45, max_retries=2)
    sql = (llm.invoke(prompt).content or "").strip()
    # Some models may wrap with ```sql
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()
    return sql


def repair_sql(
    question: str,
    schema_text: str,
    column_mapping: Dict[str, str],
    previous_sql: str,
    error_message: str,
) -> str:
    api_key = get_groq_api_key()
    if not api_key.strip():
        raise ValueError("Missing GROQ_API_KEY (required to generate SQL from natural language).")

    mapping_hint = "\n".join([f"- {k} -> {v}" for k, v in list(column_mapping.items())[:40]])
    if len(column_mapping) > 40:
        mapping_hint += "\n- (mapping truncated)"

    prompt = f"""
You generated invalid DuckDB SQL. Fix it.

Rules (must follow):
- Output ONLY a single DuckDB SQL SELECT query. No markdown, no explanation.
- Use ONLY the table named {TABLE_NAME}.
- Must be a SELECT query (no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/ATTACH).
- Ensure SQL is valid DuckDB and runs without errors.
- If selecting any non-aggregated column, include it in GROUP BY.
- Prefer GROUP BY + conditional aggregation (FILTER/CASE) over UNION.
- If the question asks for a rate/percentage, compute it (percentage 0-100) and alias it clearly.

Schema:
{schema_text}

Original->Safe column mapping (use safe names in SQL):
{mapping_hint}

User question:
{question}

Previous SQL:
{previous_sql}

DuckDB error:
{error_message}
""".strip()

    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=api_key, timeout=45, max_retries=2)
    sql = (llm.invoke(prompt).content or "").strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()
    return sql


def execute_with_retries(
    conn: duckdb.DuckDBPyConnection,
    question: str,
    schema_text: str,
    column_mapping: Dict[str, str],
    max_attempts: int = 3,
) -> Tuple[str, pd.DataFrame, float, int]:
    """
    Returns: (final_sql, df_result, elapsed_ms, attempts_used)
    """
    last_error: Optional[Exception] = None
    sql = ""
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt == 1:
                sql = generate_sql(question, schema_text, column_mapping)
            else:
                sql = repair_sql(question, schema_text, column_mapping, sql, str(last_error))

            parsed = _validate_select_only(sql)
            _enforce_table_whitelist(parsed, sql)
            sql = _ensure_limit(sql, parsed)

            t0 = time.perf_counter()
            df_result = conn.execute(sql).df()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return sql, df_result, elapsed_ms, attempt
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"Failed to generate an executable SQL query after {max_attempts} attempts. Last error: {last_error}")


@dataclass
class QueryResponse:
    answer: str
    sql: str
    explanation: str
    result: pd.DataFrame


def summarize_result(question: str, df: pd.DataFrame) -> str:
    if df.empty:
        return "No rows matched the question, so the dataset does not contain enough data to answer it."

    if df.shape == (1, 1):
        col = df.columns[0]
        val = df.iloc[0, 0]
        return f"{col}: {val}"

    if df.shape[0] == 1 and df.shape[1] <= 8:
        parts = []
        for col in df.columns:
            parts.append(f"- {col}: {df.iloc[0][col]}")
        return "Result (single row):\n" + "\n".join(parts)

    # Heuristic: 2 columns where second is numeric => show top rows
    if df.shape[1] == 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
        top = df.head(10).copy()
        rows = "\n".join([f"- {top.iloc[i,0]}: {top.iloc[i,1]}" for i in range(len(top))])
        suffix = "" if len(df) <= 10 else f"\n(Showing 10 of {len(df)} rows.)"
        return f"Top results:\n{rows}{suffix}"

    return f"Query returned {len(df)} rows × {len(df.columns)} columns. See the table below for exact values."


def build_explanation(sql: str, df: pd.DataFrame) -> str:
    return (
        f"Executed the SQL query against the uploaded dataset table `{TABLE_NAME}` "
        f"and used the returned {len(df)} rows to produce the answer (no external information)."
    )


def maybe_render_chart(df: pd.DataFrame) -> None:
    if df.empty or df.shape[1] < 2:
        return
    if alt is None:
        return

    x_col = df.columns[0]
    y_col = df.columns[1]

    if not pd.api.types.is_numeric_dtype(df[y_col]):
        return

    plot_df = df.head(500).copy()
    tooltip_cols = list(plot_df.columns[:10])

    if pd.api.types.is_datetime64_any_dtype(plot_df[x_col]):
        chart = alt.Chart(plot_df).mark_line(point=True).encode(
            x=alt.X(x_col, title=x_col),
            y=alt.Y(y_col, title=y_col),
            tooltip=tooltip_cols,
        )
    else:
        # Use bar for small cardinality, otherwise line.
        if plot_df[x_col].nunique(dropna=False) <= 30:
            chart = alt.Chart(plot_df).mark_bar().encode(
                x=alt.X(x_col, sort="-y", title=x_col),
                y=alt.Y(y_col, title=y_col),
                tooltip=tooltip_cols,
            )
        else:
            chart = alt.Chart(plot_df).mark_line(point=False).encode(
                x=alt.X(x_col, title=x_col),
                y=alt.Y(y_col, title=y_col),
                tooltip=tooltip_cols,
            )

    st.altair_chart(chart, width="stretch")


def render_chart_builder(df: pd.DataFrame, key_prefix: str) -> None:
    if alt is None:
        st.caption("Altair not available; skipping visualization.")
        return
    if df is None or df.empty or df.shape[1] < 2:
        st.caption("Not applicable for this result shape.")
        return

    cols = list(df.columns)
    numeric_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    if not numeric_cols:
        st.caption("Not applicable (no numeric columns in the result).")
        return

    default_y = numeric_cols[0]
    default_x = next((c for c in cols if c != default_y), cols[0])

    with st.expander("Customize chart", expanded=False):
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            x_col = st.selectbox("X", cols, index=cols.index(default_x), key=f"{key_prefix}_x")
        with c2:
            y_col = st.selectbox("Y (numeric)", numeric_cols, index=numeric_cols.index(default_y), key=f"{key_prefix}_y")
        with c3:
            mark = st.selectbox("Type", ["Auto", "Bar", "Line", "Area", "Scatter"], key=f"{key_prefix}_t")

        plot_df = df.head(1000).copy()
        tooltip_cols = list(plot_df.columns[:10])

        if mark == "Auto":
            # Delegate to heuristic chart.
            try:
                maybe_render_chart(plot_df[[x_col, y_col]])
            except Exception:
                st.caption("Not applicable for this result shape.")
            return

        if mark == "Bar":
            chart = alt.Chart(plot_df).mark_bar().encode(
                x=alt.X(x_col, sort="-y"),
                y=alt.Y(y_col),
                tooltip=tooltip_cols,
            )
        elif mark == "Line":
            chart = alt.Chart(plot_df).mark_line(point=True).encode(
                x=alt.X(x_col),
                y=alt.Y(y_col),
                tooltip=tooltip_cols,
            )
        elif mark == "Area":
            chart = alt.Chart(plot_df).mark_area(opacity=0.5).encode(
                x=alt.X(x_col),
                y=alt.Y(y_col),
                tooltip=tooltip_cols,
            )
        elif mark == "Scatter":
            chart = alt.Chart(plot_df).mark_circle(size=70, opacity=0.75).encode(
                x=alt.X(x_col),
                y=alt.Y(y_col),
                tooltip=tooltip_cols,
            )
        else:
            st.caption("Not applicable.")
            return

        st.altair_chart(chart, width="stretch")


def render_response_sections(answer: str, sql: str, explanation: str, df: pd.DataFrame) -> None:
    st.markdown("### 1) Data-backed Answer")
    st.markdown(answer)

    st.markdown("### 2) Result insights")
    render_result_insights(df)

    st.markdown("### 3) SQL Query used")
    st.code(sql, language="sql")

    st.markdown("### 4) Explanation (how the answer was derived)")
    st.write(explanation)

    st.markdown("### 5) Visualization (if applicable)")
    if df.empty:
        st.caption("Not applicable (no rows returned).")
        return
    # Auto chart first, then allow customization.
    try:
        maybe_render_chart(df)
    except Exception:
        st.caption("Not applicable for this result shape.")
    render_chart_builder(df, key_prefix=f"viz_{uuid.uuid4().hex}")


def render_result_download(df: pd.DataFrame, key_prefix: str) -> None:
    if df is None or df.empty:
        return
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download result as CSV",
        data=csv_bytes,
        file_name="query_result.csv",
        mime="text/csv",
        key=f"{key_prefix}_download",
        width="content",
    )

st.markdown(
    """
<style>
/* ---------- Base polish ---------- */
html, body, [class*="css"]  {
  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
}
.block-container { padding-top: 1.25rem; padding-bottom: 2.5rem; }

/* Reduce Streamlit chrome */
header[data-testid="stHeader"] { background: rgba(11, 18, 32, 0.55); backdrop-filter: blur(10px); }
div[data-testid="stToolbar"] { visibility: hidden; height: 0px; position: fixed; }

/* ---------- Hero ---------- */
.hero {
  position: relative;
  padding: 18px 18px 14px 18px;
  border-radius: 18px;
  background: radial-gradient(1200px 400px at 20% 0%, rgba(110,231,183,0.18), rgba(0,0,0,0)),
              radial-gradient(900px 380px at 90% 10%, rgba(59,130,246,0.18), rgba(0,0,0,0)),
              linear-gradient(180deg, rgba(15,26,51,0.95), rgba(15,26,51,0.65));
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 18px 60px rgba(0,0,0,0.35);
  overflow: hidden;
  animation: fadeUp 560ms ease-out both;
}
.hero:before {
  content: "";
  position: absolute;
  inset: -60px;
  background: conic-gradient(from 180deg, rgba(110,231,183,0.0), rgba(110,231,183,0.12), rgba(59,130,246,0.10), rgba(110,231,183,0.0));
  filter: blur(24px);
  opacity: 0.7;
  animation: floatGlow 10s ease-in-out infinite;
}
.hero > * { position: relative; }
.hero-title {
  font-size: 28px;
  font-weight: 820;
  letter-spacing: -0.02em;
  line-height: 1.08;
  margin: 0 0 6px 0;
}
.hero-sub {
  color: rgba(230,237,247,0.78);
  font-size: 13.5px;
  margin: 0;
}
.badge {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid rgba(255,255,255,0.10);
  background: rgba(11,18,32,0.55);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #6EE7B7;
  box-shadow: 0 0 0 0 rgba(110,231,183,0.6);
  animation: pulse 1.8s ease-out infinite;
}

/* ---------- Cards / surfaces ---------- */
.surface {
  border: 1px solid rgba(255,255,255,0.08);
  background: rgba(15,26,51,0.55);
  border-radius: 16px;
  padding: 14px 14px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.25);
  animation: fadeUp 520ms ease-out both;
}
.surface h3 { margin: 0 0 6px 0; font-size: 14px; }
.muted { color: rgba(230,237,247,0.72); font-size: 12.5px; }

/* ---------- Buttons / inputs ---------- */
button[kind="primary"] {
  border-radius: 12px !important;
  background: linear-gradient(90deg, rgba(110,231,183,1), rgba(59,130,246,0.95)) !important;
  border: 0 !important;
}
button:hover { transform: translateY(-1px); transition: transform 150ms ease; }

div[data-testid="stChatInput"] textarea {
  border-radius: 14px !important;
  border: 1px solid rgba(255,255,255,0.10) !important;
  background: rgba(11,18,32,0.55) !important;
}

/* ---------- Tabs ---------- */
button[data-baseweb="tab"] {
  border-radius: 12px;
  padding: 10px 14px;
}
button[data-baseweb="tab"][aria-selected="true"] {
  background: rgba(110,231,183,0.12) !important;
  border: 1px solid rgba(110,231,183,0.20) !important;
}

/* ---------- Animations ---------- */
@keyframes fadeUp { from { opacity: 0; transform: translateY(10px);} to {opacity: 1; transform: translateY(0);} }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(110,231,183,0.50);} 70% { box-shadow: 0 0 0 10px rgba(110,231,183,0.0);} 100% { box-shadow: 0 0 0 0 rgba(110,231,183,0.0);} }
@keyframes floatGlow { 0%, 100% { transform: translate3d(0,0,0) rotate(0deg); } 50% { transform: translate3d(22px,-10px,0) rotate(10deg);} }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="hero">
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
    <div>
      <div class="hero-title">Conversational Data Intelligence</div>
      <p class="hero-sub">Dataset-agnostic analytics: NL → DuckDB SQL → executed results → grounded answer (no external facts).</p>
    </div>
    <div class="badge"><span class="dot"></span><span>Grounded mode: ON</span></div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown("")


if "dataset_loaded" not in st.session_state:
    st.session_state.dataset_loaded = False
if "column_mapping" not in st.session_state:
    st.session_state.column_mapping = {}
if "schema_text" not in st.session_state:
    st.session_state.schema_text = ""
if "chat" not in st.session_state:
    st.session_state.chat = []
if "query_log" not in st.session_state:
    st.session_state.query_log = []
if "saved_questions" not in st.session_state:
    st.session_state.saved_questions = []
if "base_df" not in st.session_state:
    st.session_state.base_df = None
if "active_df" not in st.session_state:
    st.session_state.active_df = None
if "active_source" not in st.session_state:
    st.session_state.active_source = "No dataset loaded"
if "suggested_questions" not in st.session_state:
    st.session_state.suggested_questions = []
if "active_filters" not in st.session_state:
    st.session_state.active_filters = []
if "last_load_error" not in st.session_state:
    st.session_state.last_load_error = ""
if "default_dataset_attempted" not in st.session_state:
    st.session_state.default_dataset_attempted = False
if "is_authenticated" not in st.session_state:
    st.session_state.is_authenticated = False
if "auth_last_active_at" not in st.session_state:
    st.session_state.auth_last_active_at = None
if "auth_error" not in st.session_state:
    st.session_state.auth_error = ""

enforce_authentication()


st.sidebar.header("Dataset")
if "session_groq_key" not in st.session_state:
    st.session_state.session_groq_key = ""

effective_key = get_groq_api_key()
if effective_key:
    os.environ["GROQ_API_KEY"] = effective_key
else:
    st.sidebar.warning("Missing `GROQ_API_KEY`. Create `.env` from `.env.example` or set it below for this session.")
    with st.sidebar.expander("Set API key (session only)", expanded=False):
        st.caption("Stored only in this Streamlit session (not written to disk).")
        entered = st.text_input("GROQ_API_KEY", type="password", placeholder="Paste your Groq API key")
        if st.button("Use key for this session", width="stretch") and entered.strip():
            st.session_state.session_groq_key = entered.strip()
            os.environ["GROQ_API_KEY"] = entered.strip()
            st.rerun()
if effective_key:
    with st.sidebar.expander("API key", expanded=False):
        st.caption("Key detected from environment or Streamlit secrets. Session entry is optional.")
        if st.button("Clear session key", width="stretch"):
            st.session_state.session_groq_key = ""
            st.rerun()

with st.sidebar.expander("Diagnostics", expanded=False):
    key_source = "missing"
    env_val = os.getenv("GROQ_API_KEY")
    if env_val is not None and env_val.strip():
        key_source = "env"
    elif env_val is not None and not env_val.strip():
        key_source = "env_empty"
    else:
        try:
            if str(st.secrets.get("GROQ_API_KEY", "")).strip():
                key_source = "secrets"
        except Exception:
            pass
        # Check `.env` file fallback
        if key_source == "missing":
            try:
                from dotenv import dotenv_values

                vals = dotenv_values(_DOTENV_PATH)
                if str(vals.get("GROQ_API_KEY", "") or "").strip():
                    key_source = "dotenv_file"
            except Exception:
                pass

        if key_source == "missing" and (st.session_state.get("session_groq_key") or "").strip():
            key_source = "session"

    st.write(
        {
            "dataset_loaded": bool(st.session_state.dataset_loaded),
            "groq_api_key": "present" if bool(get_groq_api_key()) else "missing",
            "key_source": key_source,
            "auth_enabled": bool(get_app_password()),
            "session_timeout_minutes": get_auth_timeout_minutes(),
        }
    )
    if bool(get_app_password()) and st.button("Sign out", width="stretch"):
        st.session_state.is_authenticated = False
        st.session_state.auth_last_active_at = None
        st.rerun()
st.sidebar.caption("This app answers strictly from your dataset by generating and executing DuckDB SQL.")
uploaded_file = st.sidebar.file_uploader("Upload CSV / XLSX / JSON", type=["csv", "xlsx", "xls", "json"])
url_input = st.sidebar.text_input(
    "Or paste dataset URL (CSV/JSON, GitHub link supported)",
    value=st.session_state.get("dataset_url", DEFAULT_SAMPLE_URL),
)
url_input = normalize_url(url_input)

if (
    not st.session_state.dataset_loaded
    and not st.session_state.default_dataset_attempted
    and uploaded_file is None
):
    try:
        df_raw = load_dataset(url=DEFAULT_SAMPLE_URL)
        df_safe, mapping = make_safe_columns(df_raw)
        st.session_state.base_df = df_safe.copy()
        activate_dataset(df_safe, mapping, "Remote dataset")
        st.session_state.active_filters = []
        st.session_state.suggested_questions = infer_suggested_questions(df_safe)
        st.session_state.last_load_error = ""
    except Exception as e:
        st.session_state.last_load_error = str(e)
    finally:
        st.session_state.default_dataset_attempted = True

col_a, col_b = st.sidebar.columns(2)
with col_a:
    load_sample = st.button("Load sample", help="Loads the provided Telco churn CSV from GitHub", width="stretch")
with col_b:
    load_custom = st.button("Load dataset", type="primary", width="stretch")

if load_sample:
    st.session_state["dataset_url"] = DEFAULT_SAMPLE_URL
    url_input = DEFAULT_SAMPLE_URL

if load_custom or load_sample:
    try:
        chosen_url = DEFAULT_SAMPLE_URL if load_sample else (None if uploaded_file is not None else url_input)
        st.session_state["dataset_url"] = chosen_url or url_input
        df_raw = load_dataset(uploaded_file, chosen_url)
        df_safe, mapping = make_safe_columns(df_raw)
        st.session_state.base_df = df_safe.copy()
        source_label = "Uploaded file" if uploaded_file is not None and not load_sample else "Remote dataset"
        activate_dataset(df_safe, mapping, source_label)
        st.session_state.active_filters = []
        st.session_state.suggested_questions = infer_suggested_questions(df_safe)
        st.session_state.chat = []
        st.session_state.last_load_error = ""
        st.session_state.default_dataset_attempted = True
        st.sidebar.success(f"Loaded {len(df_safe):,} rows × {len(df_safe.columns):,} columns ✅")
    except Exception as e:
        st.session_state.dataset_loaded = False
        st.session_state.last_load_error = str(e)
        st.sidebar.error(str(e))

if st.session_state.dataset_loaded:
    with st.sidebar.expander("Schema", expanded=False):
        st.markdown(st.session_state.schema_text)
    st.sidebar.caption(f"Working dataset: {st.session_state.active_source}")

tab_chat, tab_explorer, tab_profile, tab_ops = st.tabs(["Chat", "Explorer", "Dataset profile", "Operations"])

with tab_chat:
    st.subheader("Chat")
    can_chat = bool(get_groq_api_key()) and st.session_state.dataset_loaded

    if not st.session_state.dataset_loaded:
        st.warning("Load a dataset from the sidebar or use the button below before asking questions.")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("Load sample dataset here", width="stretch"):
                try:
                    df_raw = load_dataset(url=DEFAULT_SAMPLE_URL)
                    df_safe, mapping = make_safe_columns(df_raw)
                    st.session_state.base_df = df_safe.copy()
                    activate_dataset(df_safe, mapping, "Remote dataset")
                    st.session_state.active_filters = []
                    st.session_state.suggested_questions = infer_suggested_questions(df_safe)
                    st.session_state.chat = []
                    st.session_state.last_load_error = ""
                    st.rerun()
                except Exception as e:
                    st.session_state.last_load_error = str(e)
                    st.error(f"Dataset load failed: {e}")
        with c2:
            st.caption("Tip: if you uploaded a file, click `Load dataset` in the sidebar after selecting it.")

    if st.session_state.last_load_error:
        st.error(f"Last dataset load error: {st.session_state.last_load_error}")

    quick = st.container()
    with quick:
        st.markdown('<div class="surface">', unsafe_allow_html=True)
        st.markdown("**Quick questions (click to run)**")
        st.caption("These run the same grounded pipeline (SQL → execute → answer).")
        suggested = st.session_state.get("suggested_questions") or [
            "What is the churn rate by gender?",
            "What is the churn rate by contract type?",
            "What is the average tenure for churned vs non-churned customers?",
            "Show the top 10 customers by MonthlyCharges.",
        ]
        quick_cols = st.columns(4)
        for idx, prompt in enumerate(suggested[:4]):
            with quick_cols[idx]:
                if st.button(prompt[:28] + ("..." if len(prompt) > 28 else ""), width="stretch", disabled=not can_chat, key=f"quick_{idx}"):
                    st.session_state["pending_question"] = prompt
        st.markdown("</div>", unsafe_allow_html=True)

    for item in st.session_state.chat:
        with st.chat_message(item["role"]):
            if item["role"] == "user":
                st.markdown(item["content"])
                continue

            msg_id = item.get("id") or uuid.uuid4().hex
            if item.get("meta"):
                st.caption(item["meta"])

            sql = item.get("sql")
            explanation = item.get("explanation")
            df = item.get("df")

            if sql and explanation and isinstance(df, pd.DataFrame):
                render_response_sections(answer=item["content"], sql=sql, explanation=explanation, df=df)
                if not df.empty:
                    st.dataframe(df, width="stretch")
                    render_result_download(df, key_prefix=msg_id)
            else:
                st.markdown(item["content"])

    if not get_groq_api_key():
        st.info("Add `GROQ_API_KEY` (sidebar) to enable natural-language chat → SQL generation.")
        user_q = None
    elif not st.session_state.dataset_loaded:
        st.info("Dataset is not ready yet. Load a dataset first, then ask questions.")
        user_q = None
    else:
        user_q = st.chat_input(
            "Ask a question about your dataset (e.g., churn rate by gender, revenue by month, top 10 customers)"
        )

    if not user_q and st.session_state.get("pending_question"):
        user_q = st.session_state.pop("pending_question")

    st.markdown("**Reliable input box**")
    st.caption("Use this if the chat box above is hard to type into on the in-app browser.")
    with st.form("fallback_form", clear_on_submit=True):
        fallback_q = st.text_input(
            "Question",
            key="fallback_question",
            placeholder="Type here and click Ask.",
            disabled=not bool(get_groq_api_key()),
        )
        submitted = st.form_submit_button(
            "Ask",
            disabled=not bool(get_groq_api_key()) or not st.session_state.dataset_loaded,
        )
    if submitted and fallback_q.strip():
        user_q = fallback_q.strip()

    if user_q:
        if not st.session_state.dataset_loaded:
            st.warning("Load a dataset first using the sidebar.")
        else:
            msg_id = uuid.uuid4().hex
            st.session_state.chat.append({"role": "user", "content": user_q, "id": msg_id})

            conn = get_duckdb_connection()
            available_cols = list_columns(conn)
            with st.chat_message("assistant"):
                status = st.status("Working…", expanded=True)
                status.write("1/3 Generating DuckDB SQL")
                with st.spinner("Generating SQL → executing → summarizing…"):
                    try:
                        status.write("2/3 Validating + executing in DuckDB (auto-repair on errors)")
                        sql, df_result, elapsed_ms, attempts = execute_with_retries(
                            conn=conn,
                            question=user_q,
                            schema_text=st.session_state.schema_text,
                            column_mapping=st.session_state.column_mapping,
                            max_attempts=3,
                        )
                        status.write(f"3/3 Summarizing results (grounded). Attempts: {attempts}")

                        # Keep chat state lightweight.
                        df_for_chat = df_result.head(MAX_RESULT_ROWS).copy()

                        answer = summarize_result(user_q, df_result)
                        explanation = (
                            build_explanation(sql, df_result)
                            + f" Rows returned: {len(df_result):,}. Query runtime: {elapsed_ms:.1f} ms."
                        )
                        meta = f"Rows returned: {len(df_result):,}. Runtime: {elapsed_ms:.1f} ms."

                        render_response_sections(answer=answer, sql=sql, explanation=explanation, df=df_for_chat)
                        if not df_for_chat.empty:
                            st.dataframe(df_for_chat, width="stretch")
                            render_result_download(df_for_chat, key_prefix=msg_id)

                        st.session_state.chat.append(
                            {
                                "role": "assistant",
                                "content": answer,
                                "question": user_q,
                                "sql": sql,
                                "df": df_for_chat,
                                "explanation": explanation,
                                "meta": meta,
                                "id": msg_id,
                            }
                        )
                        st.session_state.query_log.append(
                            {
                                "id": msg_id,
                                "question": user_q,
                                "content": answer,
                                "sql": sql,
                                "df": df_for_chat,
                                "explanation": explanation,
                                "meta": meta,
                                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        )
                        status.update(label="Done", state="complete", expanded=False)
                    except Exception as e:
                        attempted_sql = locals().get("sql", "").strip()  # best-effort
                        msg = str(e)
                        lower = msg.lower()
                        missing_cols_expl = _explain_missing_columns(msg, available_cols)

                        if missing_cols_expl:
                            answer = "I can’t answer because the required column(s) are not present in the dataset."
                            explanation = f"{missing_cols_expl}. DuckDB error: {msg}"
                        elif "missing groq_api_key" in lower:
                            answer = "I can’t answer because the API key needed to generate SQL is missing."
                            explanation = "No SQL was generated. Set `GROQ_API_KEY` (sidebar or `.env`) and retry."
                        elif "only select queries are allowed" in lower or "multiple statements" in lower:
                            answer = "I can’t answer because the generated query was not a safe SELECT-only query."
                            explanation = f"Safety validation rejected the SQL before execution. Reason: {msg}"
                        else:
                            answer = "I can’t answer from the dataset due to an execution/validation error."
                            explanation = f"Generated SQL, then attempted to validate/execute it in DuckDB. Error: {msg}"

                        safe_sql = attempted_sql or "-- (no SQL generated)"
                        render_response_sections(answer=answer, sql=safe_sql, explanation=explanation, df=pd.DataFrame())
                        st.error(msg)

                        st.session_state.chat.append(
                            {
                                "role": "assistant",
                                "content": answer,
                                "question": user_q,
                                "sql": safe_sql,
                                "df": pd.DataFrame(),
                                "explanation": explanation,
                                "id": msg_id,
                            }
                        )
                        st.session_state.query_log.append(
                            {
                                "id": msg_id,
                                "question": user_q,
                                "content": answer,
                                "sql": safe_sql,
                                "df": pd.DataFrame(),
                                "explanation": explanation,
                                "meta": "Failed safely",
                                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        )
                        status.update(label="Failed safely", state="error", expanded=False)

with tab_explorer:
    st.subheader("Explorer workbench")
    if not st.session_state.dataset_loaded or st.session_state.base_df is None:
        st.info("Load a dataset first to unlock interactive exploration and filters.")
    else:
        base_df = st.session_state.base_df.copy()
        active_df = st.session_state.active_df.copy()
        health = build_dataset_health(active_df)

        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Active rows", f"{health['rows']:,}")
        s2.metric("Quality score", f"{health['quality_score']}/100")
        s3.metric("Completeness", f"{health['completeness_pct']}%")
        s4.metric("Duplicates", f"{health['duplicate_rows']:,}")
        s5.metric("Memory", f"{health['memory_mb']} MB")
        st.caption(f"Current working dataset: {st.session_state.active_source}")

        with st.expander("Interactive filters", expanded=True):
            filter_cols = st.multiselect(
                "Choose up to 3 columns to filter",
                list(base_df.columns),
                default=[f["column"] for f in st.session_state.active_filters if f["column"] in base_df.columns][:3],
                max_selections=3,
            )
            pending_filters: List[Dict[str, Any]] = []
            for col in filter_cols:
                series = base_df[col]
                if is_numeric_dtype(series):
                    numeric_series = pd.to_numeric(series, errors="coerce").dropna()
                    if numeric_series.empty:
                        continue
                    low_default = float(numeric_series.min())
                    high_default = float(numeric_series.max())
                    value = st.slider(
                        f"{col} range",
                        min_value=low_default,
                        max_value=high_default,
                        value=(low_default, high_default),
                        key=f"filter_range_{col}",
                    )
                    pending_filters.append({"column": col, "kind": "numeric_range", "value": value})
                else:
                    top_values = [str(v) for v in series.dropna().astype(str).value_counts().head(25).index.tolist()]
                    if len(top_values) <= 25:
                        chosen = st.multiselect(f"{col} values", top_values, key=f"filter_cat_{col}")
                        pending_filters.append({"column": col, "kind": "categorical_values", "value": chosen})
                    else:
                        term = st.text_input(f"{col} contains", key=f"filter_text_{col}")
                        pending_filters.append({"column": col, "kind": "text_contains", "value": term})

            f1, f2 = st.columns(2)
            with f1:
                if st.button("Apply filters to working dataset", type="primary", width="stretch"):
                    mask = build_filter_mask(base_df, pending_filters)
                    filtered_df = base_df.loc[mask].copy()
                    activate_dataset(filtered_df, st.session_state.column_mapping, "Filtered dataset view")
                    st.session_state.active_filters = pending_filters
                    st.session_state.suggested_questions = infer_suggested_questions(filtered_df)
                    st.success(f"Activated filtered dataset with {len(filtered_df):,} rows.")
                    st.rerun()
            with f2:
                if st.button("Reset to full dataset", width="stretch"):
                    activate_dataset(base_df, st.session_state.column_mapping, "Full dataset view")
                    st.session_state.active_filters = []
                    st.session_state.suggested_questions = infer_suggested_questions(base_df)
                    st.success("Restored the full dataset.")
                    st.rerun()

        st.markdown("**Suggested business questions**")
        sugg_cols = st.columns(2)
        for idx, prompt in enumerate(st.session_state.suggested_questions[:8]):
            with sugg_cols[idx % 2]:
                if st.button(prompt, key=f"suggestion_{idx}", width="stretch"):
                    st.session_state["pending_question"] = prompt
                    st.rerun()

        st.markdown("**Active dataset preview**")
        st.dataframe(active_df.head(100), width="stretch")

with tab_profile:
    st.subheader("Dataset profile")
    if not st.session_state.dataset_loaded:
        st.info("Load a dataset using the sidebar to see profiling details.")
    else:
        conn = get_duckdb_connection()
        active_df = st.session_state.active_df.copy()
        row_count = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
        types = column_type_map(conn)
        col_count = len(types)
        health = build_dataset_health(active_df)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Rows", f"{row_count:,}")
        k2.metric("Columns", f"{col_count:,}")
        try:
            null_cells = conn.execute(
                "SELECT SUM(" + " + ".join([f"CASE WHEN {c} IS NULL THEN 1 ELSE 0 END" for c in types.keys()]) + f") FROM {TABLE_NAME}"
            ).fetchone()[0]
            k3.metric("Null cells", f"{int(null_cells or 0):,}")
        except Exception:
            k3.metric("Null cells", "—")
        k4.metric("Quality score", f"{health['quality_score']}/100")

        with st.expander("Executive data health", expanded=True):
            h1, h2, h3, h4 = st.columns(4)
            h1.metric("Completeness", f"{health['completeness_pct']}%")
            h2.metric("Duplicate rows", f"{health['duplicate_rows']:,}")
            h3.metric("Columns with nulls", f"{health['columns_with_nulls']:,}")
            h4.metric("Dataset memory", f"{health['memory_mb']} MB")
            for issue in health["issues"]:
                st.caption(issue)

        st.markdown("**Preview (first 50 rows)**")
        st.dataframe(conn.execute(f"SELECT * FROM {TABLE_NAME} LIMIT 50").df(), width="stretch")

        st.markdown("**Column mapping (original → safe)**")
        mapping = st.session_state.column_mapping
        inv = inverse_mapping(mapping)
        map_df = pd.DataFrame(
            [{"original": original, "safe": safe} for original, safe in mapping.items()]
        )
        st.dataframe(map_df, width="stretch", hide_index=True)

        st.markdown("**Data quality (missing values by column)**")
        if alt is None:
            st.caption("Altair not available; skipping charts.")
        else:
            null_counts = []
            for c in types.keys():
                n = conn.execute(f"SELECT SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) FROM {TABLE_NAME}").fetchone()[0]
                null_counts.append({"column": c, "nulls": int(n or 0)})
            null_df = pd.DataFrame(null_counts).sort_values("nulls", ascending=False)
            null_chart = (
                alt.Chart(null_df)
                .mark_bar()
                .encode(x=alt.X("nulls:Q", title="Null count"), y=alt.Y("column:N", sort="-x", title="Column"))
            )
            st.altair_chart(null_chart, width="stretch")

        safe_cols = list(types.keys())
        selected = st.selectbox("Analyze a column", safe_cols)
        if selected:
            original = inv.get(selected, selected)
            st.caption(f"Selected: `{selected}` (original: `{original}`), type: `{types[selected]}`")
            analysis = analyze_column(conn, selected, types[selected])
            st.write(f"Nulls: {analysis['nulls']:,}")
            if "top_values" in analysis:
                st.markdown("**Top values (up to 10)**")
                st.dataframe(analysis["top_values"], width="stretch", hide_index=True)
                if alt is not None and not analysis["top_values"].empty:
                    chart = (
                        alt.Chart(analysis["top_values"])
                        .mark_bar()
                        .encode(x=alt.X("count:Q", title="Count"), y=alt.Y("value:N", sort="-x", title=selected))
                    )
                    st.altair_chart(chart, width="stretch")
            else:
                st.markdown("**Numeric stats**")
                st.write({"min": analysis.get("min"), "max": analysis.get("max"), "avg": analysis.get("avg")})
                if alt is not None:
                    # Histogram for numeric columns
                    df_hist = conn.execute(f"SELECT {selected} AS v FROM {TABLE_NAME} WHERE {selected} IS NOT NULL").df()
                    if not df_hist.empty:
                        hist = (
                            alt.Chart(df_hist)
                            .mark_bar()
                            .encode(
                                x=alt.X("v:Q", bin=alt.Bin(maxbins=30), title=selected),
                                y=alt.Y("count():Q", title="Count"),
                            )
                        )
                        st.altair_chart(hist, width="stretch")

        st.markdown("**Column catalog**")
        catalog_rows = []
        for col in active_df.columns:
            catalog_rows.append(
                {
                    "column": col,
                    "dtype": str(active_df[col].dtype),
                    "null_pct": round(float(active_df[col].isna().mean() * 100), 2),
                    "distinct_values": int(active_df[col].nunique(dropna=False)),
                    "sample": str(active_df[col].dropna().iloc[0])[:80] if not active_df[col].dropna().empty else "",
                }
            )
        st.dataframe(pd.DataFrame(catalog_rows), width="stretch", hide_index=True)

with tab_ops:
    st.subheader("Operations")
    o1, o2 = st.columns([2, 1])
    with o1:
        st.markdown("**Saved questions**")
        if st.session_state.saved_questions:
            for idx, question in enumerate(st.session_state.saved_questions):
                c1, c2 = st.columns([5, 1])
                with c1:
                    if st.button(question, key=f"saved_q_{idx}", width="stretch"):
                        st.session_state["pending_question"] = question
                        st.rerun()
                with c2:
                    if st.button("Delete", key=f"saved_q_delete_{idx}", width="stretch"):
                        st.session_state.saved_questions.pop(idx)
                        st.rerun()
        else:
            st.caption("No saved questions yet. Save useful prompts from query history below.")

    with o2:
        st.markdown("**Workspace controls**")
        if st.button("Save latest question", width="stretch", disabled=not bool(st.session_state.query_log)):
            latest_question = st.session_state.query_log[-1]["question"]
            if latest_question not in st.session_state.saved_questions:
                st.session_state.saved_questions.append(latest_question)
            st.rerun()
        if st.button("Clear chat history", width="stretch"):
            st.session_state.chat = []
            st.rerun()
        if st.button("Clear query log", width="stretch"):
            st.session_state.query_log = []
            st.rerun()

    st.markdown("**Query history**")
    if not st.session_state.query_log:
        st.caption("Run a few questions to build an audit trail.")
    else:
        for entry in reversed(st.session_state.query_log[-12:]):
            with st.expander(f"{entry['created_at']} | {entry['question']}", expanded=False):
                st.write(entry["content"])
                st.code(entry["sql"], language="sql")
                st.caption(entry.get("meta", ""))
                a1, a2, a3 = st.columns(3)
                with a1:
                    if st.button("Rerun question", key=f"rerun_{entry['id']}", width="stretch"):
                        st.session_state["pending_question"] = entry["question"]
                        st.rerun()
                with a2:
                    if st.button("Save question", key=f"save_{entry['id']}", width="stretch"):
                        if entry["question"] not in st.session_state.saved_questions:
                            st.session_state.saved_questions.append(entry["question"])
                        st.rerun()
                with a3:
                    st.download_button(
                        "Download package",
                        data=build_query_package(entry),
                        file_name=f"analysis_{entry['id']}.zip",
                        mime="application/zip",
                        key=f"download_pkg_{entry['id']}",
                        width="stretch",
                    )
