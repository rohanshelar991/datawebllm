import io
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import duckdb
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from sqlglot import exp, parse_one

try:
    import altair as alt
except Exception:  # pragma: no cover
    alt = None  # type: ignore[assignment]


TABLE_NAME = "data_table"
MAX_RESULT_ROWS = 500
DEFAULT_SAMPLE_URL = "https://github.com/Geo-y20/Telco-Customer-Churn-/blob/main/Telco%20Customer%20Churn.csv"


load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)

st.set_page_config(page_title="Conversational Data Intelligence", layout="wide")


@st.cache_resource
def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(database=":memory:")


def get_groq_api_key() -> str:
    # Priority:
    # 1) Environment variable (best for deployment)
    # 2) Streamlit secrets (Streamlit Community Cloud / managed deploy)
    # 3) Session-only key (manual fallback)
    env_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if env_key:
        return env_key

    try:
        secrets_key = str(st.secrets.get("GROQ_API_KEY", "")).strip()
        if secrets_key:
            return secrets_key
    except Exception:
        pass

    return str(st.session_state.get("session_groq_key", "") or "").strip()


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
        resp = requests.get(raw_url, timeout=30)
        resp.raise_for_status()
        # Try CSV first (most common for this assignment)
        try:
            return pd.read_csv(io.StringIO(resp.text))
        except Exception:
            return pd.read_json(io.StringIO(resp.text))

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
    if parsed.find(lambda node: isinstance(node, illegal)):  # type: ignore[arg-type]
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
- If the question is ambiguous, pick the simplest reasonable interpretation using available columns.

Schema:
{schema_text}

Original->Safe column mapping (use safe names in SQL):
{mapping_hint}

User question:
{question}
""".strip()

    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=api_key)
    sql = (llm.invoke(prompt).content or "").strip()
    # Some models may wrap with ```sql
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE).strip()
    return sql


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

    plot_df = df.head(200).copy()
    if pd.api.types.is_datetime64_any_dtype(plot_df[x_col]):
        chart = alt.Chart(plot_df).mark_line(point=True).encode(x=x_col, y=y_col, tooltip=list(plot_df.columns))
    else:
        # Use bar for small cardinality, otherwise line.
        if plot_df[x_col].nunique(dropna=False) <= 30:
            chart = alt.Chart(plot_df).mark_bar().encode(x=alt.X(x_col, sort="-y"), y=y_col, tooltip=list(plot_df.columns))
        else:
            chart = alt.Chart(plot_df).mark_line(point=False).encode(x=x_col, y=y_col, tooltip=list(plot_df.columns))

    st.altair_chart(chart, use_container_width=True)


def render_response_sections(answer: str, sql: str, explanation: str, df: pd.DataFrame) -> None:
    st.markdown("### 1) Data-backed Answer")
    st.markdown(answer)

    st.markdown("### 2) SQL Query used")
    st.code(sql, language="sql")

    st.markdown("### 3) Explanation (how the answer was derived)")
    st.write(explanation)

    st.markdown("### 4) Visualization (if applicable)")
    if df.empty:
        st.caption("Not applicable (no rows returned).")
        return
    if alt is None:
        st.caption("Altair not available; skipping visualization.")
        return
    before = st.empty()
    # Render a chart only when the result shape supports it; otherwise explicitly say N/A.
    try:
        maybe_render_chart(df)
        before.empty()
    except Exception:
        before.caption("Not applicable for this result shape.")


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
        use_container_width=False,
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
        entered = st.text_input("GROQ_API_KEY", type="password", placeholder="gsk_…")
        if st.button("Use key for this session", use_container_width=True) and entered.strip():
            st.session_state.session_groq_key = entered.strip()
            os.environ["GROQ_API_KEY"] = entered.strip()
            st.rerun()
if effective_key:
    with st.sidebar.expander("API key", expanded=False):
        st.caption("Key detected from environment or Streamlit secrets. Session entry is optional.")
        if st.button("Clear session key", use_container_width=True):
            st.session_state.session_groq_key = ""
            st.rerun()
st.sidebar.caption("This app answers strictly from your dataset by generating and executing DuckDB SQL.")
uploaded_file = st.sidebar.file_uploader("Upload CSV / XLSX / JSON", type=["csv", "xlsx", "xls", "json"])
url_input = st.sidebar.text_input(
    "Or paste dataset URL (CSV/JSON, GitHub link supported)",
    value=st.session_state.get("dataset_url", DEFAULT_SAMPLE_URL),
)
url_input = normalize_url(url_input)

col_a, col_b = st.sidebar.columns(2)
with col_a:
    load_sample = st.button("Load sample", help="Loads the provided Telco churn CSV from GitHub", use_container_width=True)
with col_b:
    load_custom = st.button("Load dataset", type="primary", use_container_width=True)

if load_sample:
    st.session_state["dataset_url"] = DEFAULT_SAMPLE_URL
    url_input = DEFAULT_SAMPLE_URL

if load_custom or load_sample:
    try:
        st.session_state["dataset_url"] = url_input
        df_raw = load_dataset(uploaded_file, url_input)
        df_safe, mapping = make_safe_columns(df_raw)
        conn = get_duckdb_connection()
        register_dataframe(conn, df_safe)
        st.session_state.dataset_loaded = True
        st.session_state.column_mapping = mapping
        st.session_state.schema_text = build_schema_text(conn)
        st.session_state.chat = []
        st.sidebar.success(f"Loaded {len(df_safe):,} rows × {len(df_safe.columns):,} columns ✅")
    except Exception as e:
        st.session_state.dataset_loaded = False
        st.sidebar.error(str(e))

if st.session_state.dataset_loaded:
    with st.sidebar.expander("Schema", expanded=False):
        st.markdown(st.session_state.schema_text)

tab_chat, tab_profile = st.tabs(["Chat", "Dataset profile"])

with tab_chat:
    st.subheader("Chat")

    quick = st.container()
    with quick:
        st.markdown('<div class="surface">', unsafe_allow_html=True)
        st.markdown("**Quick questions (click to run)**")
        st.caption("These run the same grounded pipeline (SQL → execute → answer).")
        can_chat = bool(get_groq_api_key()) and st.session_state.dataset_loaded
        q1, q2, q3, q4 = st.columns(4)
        with q1:
            if st.button("Churn rate by gender", use_container_width=True, disabled=not can_chat):
                st.session_state["pending_question"] = "What is the churn rate of male and female customers?"
        with q2:
            if st.button("Churn by contract", use_container_width=True, disabled=not can_chat):
                st.session_state["pending_question"] = "What is the churn rate by contract type?"
        with q3:
            if st.button("Avg tenure by churn", use_container_width=True, disabled=not can_chat):
                st.session_state["pending_question"] = "What is the average tenure for churned vs non-churned customers?"
        with q4:
            if st.button("Top 10 charges", use_container_width=True, disabled=not can_chat):
                st.session_state["pending_question"] = "Show the top 10 customers by MonthlyCharges."
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
                    st.dataframe(df, use_container_width=True)
                    render_result_download(df, key_prefix=msg_id)
            else:
                st.markdown(item["content"])

    if not get_groq_api_key():
        st.info("Add `GROQ_API_KEY` (sidebar) to enable natural-language chat → SQL generation.")
        user_q = None
    else:
        user_q = st.chat_input(
            "Ask a question about your dataset (e.g., churn rate by gender, revenue by month, top 10 customers)"
        )

    if not user_q and st.session_state.get("pending_question"):
        user_q = st.session_state.pop("pending_question")

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
                        sql = generate_sql(user_q, st.session_state.schema_text, st.session_state.column_mapping)
                        status.write("2/3 Validating + executing in DuckDB")
                        parsed = _validate_select_only(sql)
                        _enforce_table_whitelist(parsed, sql)
                        sql = _ensure_limit(sql, parsed)
                        t0 = time.perf_counter()
                        df_result = conn.execute(sql).df()
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        status.write("3/3 Summarizing results (grounded)")

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
                            st.dataframe(df_for_chat, use_container_width=True)
                            render_result_download(df_for_chat, key_prefix=msg_id)

                        st.session_state.chat.append(
                            {
                                "role": "assistant",
                                "content": answer,
                                "sql": sql,
                                "df": df_for_chat,
                                "explanation": explanation,
                                "meta": meta,
                                "id": msg_id,
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
                                "sql": safe_sql,
                                "df": pd.DataFrame(),
                                "explanation": explanation,
                                "id": msg_id,
                            }
                        )
                        status.update(label="Failed safely", state="error", expanded=False)

with tab_profile:
    st.subheader("Dataset profile")
    if not st.session_state.dataset_loaded:
        st.info("Load a dataset using the sidebar to see profiling details.")
    else:
        conn = get_duckdb_connection()
        row_count = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
        st.write(f"Rows: {row_count:,}")

        st.markdown("**Preview (first 50 rows)**")
        st.dataframe(conn.execute(f"SELECT * FROM {TABLE_NAME} LIMIT 50").df(), use_container_width=True)

        st.markdown("**Column mapping (original → safe)**")
        mapping = st.session_state.column_mapping
        inv = inverse_mapping(mapping)
        map_df = pd.DataFrame(
            [{"original": original, "safe": safe} for original, safe in mapping.items()]
        )
        st.dataframe(map_df, use_container_width=True, hide_index=True)

        types = column_type_map(conn)
        safe_cols = list(types.keys())
        selected = st.selectbox("Analyze a column", safe_cols)
        if selected:
            original = inv.get(selected, selected)
            st.caption(f"Selected: `{selected}` (original: `{original}`), type: `{types[selected]}`")
            analysis = analyze_column(conn, selected, types[selected])
            st.write(f"Nulls: {analysis['nulls']:,}")
            if "top_values" in analysis:
                st.markdown("**Top values (up to 10)**")
                st.dataframe(analysis["top_values"], use_container_width=True, hide_index=True)
            else:
                st.markdown("**Numeric stats**")
                st.write({"min": analysis.get("min"), "max": analysis.get("max"), "avg": analysis.get("avg")})
