import streamlit as st
import pandas as pd
import sqlite3
import requests
import io
import uuid
import matplotlib.pyplot as plt
import os
from dotenv import load_dotenv

from typing import TypedDict, List
from langgraph.graph import StateGraph
from langchain_groq import ChatGroq

# =========================
# ENV LOAD
# =========================
load_dotenv()
os.environ["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")

# =========================
# PAGE CONFIG
# =========================
st.set_page_config(page_title="Agentic Data Intelligence", layout="wide")

# =========================
# SQLITE CONNECTION (THREAD SAFE)
# =========================
@st.cache_resource
def get_connection():
    return sqlite3.connect(":memory:", check_same_thread=False)

# =========================
# PROFESSIONAL DARK UI
# =========================
st.markdown("""
<style>
body {
    background-color: #0E1117;
}
.main-title {
    font-size: 34px;
    font-weight: 700;
    color: #4CAF50;
}
.card {
    background-color: #161B22;
    padding: 25px;
    border-radius: 15px;
    box-shadow: 0px 0px 20px rgba(0,0,0,0.4);
}
.stButton>button {
    background: linear-gradient(90deg,#4CAF50,#00C853);
    color: white;
    border-radius: 8px;
    height: 3em;
    font-weight: bold;
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">📊 Agentic Data Intelligence Platform</div>', unsafe_allow_html=True)
st.markdown("---")

# =========================
# SESSION STATE
# =========================
if "df" not in st.session_state:
    st.session_state.df = None
if "schema" not in st.session_state:
    st.session_state.schema = None

# =========================
# DATA LOADER
# =========================
def load_dataset(file=None, url=None):
    if url:
        response = requests.get(url)
        content_type = response.headers.get("Content-Type", "")
        if "csv" in content_type:
            return pd.read_csv(io.StringIO(response.text))
        elif "json" in content_type:
            return pd.read_json(io.StringIO(response.text))

    if file:
        if file.name.endswith(".csv"):
            return pd.read_csv(file)
        elif file.name.endswith(".xlsx"):
            return pd.read_excel(file)
        elif file.name.endswith(".json"):
            return pd.read_json(file)

    return None


def extract_schema(df):
    return [f"{col} ({df[col].dtype})" for col in df.columns]


# =========================
# GRAPH TOOL
# =========================
def generate_graph(df_result):
    if len(df_result.columns) >= 2:
        x = df_result.iloc[:, 0]
        y = df_result.iloc[:, 1]

        filename = f"graph_{uuid.uuid4().hex}.png"

        plt.figure()
        plt.bar(x, y)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(filename)
        plt.close()

        return filename
    return None


# =========================
# SIDEBAR
# =========================
st.sidebar.header("📂 Dataset")

uploaded_file = st.sidebar.file_uploader("Upload CSV / XLSX / JSON")
url_input = st.sidebar.text_input("Or Enter Dataset URL")

if st.sidebar.button("Load Dataset"):
    df = load_dataset(uploaded_file, url_input)

    if df is not None:
        st.session_state.df = df
        st.session_state.schema = extract_schema(df)

        conn = get_connection()
        conn.execute("DROP TABLE IF EXISTS data_table")
        df.to_sql("data_table", conn, index=False, if_exists="replace")

        st.sidebar.success("Dataset Loaded Successfully ✅")
    else:
        st.sidebar.error("Failed to load dataset")

if st.session_state.schema:
    st.sidebar.subheader("📑 Schema")
    for col in st.session_state.schema:
        st.sidebar.write(col)

# =========================
# LLM INIT
# =========================
llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0)

# =========================
# LANGGRAPH STATE
# =========================
class AgentState(TypedDict):
    messages: List[str]
    sql_query: str
    sql_result: str


# =========================
# NODE 1: SQL GENERATION
# =========================
def generate_sql(state: AgentState):
    schema_text = "\n".join(st.session_state.schema)
    question = state["messages"][-1]

    prompt = f"""
You are a professional data analyst.

Dataset Schema:
{schema_text}

User Question:
{question}

Generate ONLY a valid SQLite SELECT query on table 'data_table'.
Return only SQL.
"""
    sql = llm.invoke(prompt).content.strip()
    return {"sql_query": sql}


# =========================
# NODE 2: EXECUTE SQL
# =========================
def execute_sql(state: AgentState):
    try:
        conn = get_connection()
        df_result = pd.read_sql_query(state["sql_query"], conn)
        return {"sql_result": df_result.to_json()}
    except Exception as e:
        return {"sql_result": f"SQL Error: {str(e)}"}


# =========================
# NODE 3: REPORT
# =========================
def generate_report(state: AgentState):
    prompt = f"""
Create a professional markdown data analysis report.

SQL Result:
{state['sql_result']}

Include:
- Summary
- Key Insights
- Business Interpretation
"""
    report = llm.invoke(prompt).content
    return {"messages": state["messages"] + [report]}


# =========================
# BUILD GRAPH
# =========================
workflow = StateGraph(AgentState)

workflow.add_node("generate_sql", generate_sql)
workflow.add_node("execute_sql", execute_sql)
workflow.add_node("generate_report", generate_report)

workflow.set_entry_point("generate_sql")
workflow.add_edge("generate_sql", "execute_sql")
workflow.add_edge("execute_sql", "generate_report")

app_graph = workflow.compile()

# =========================
# MAIN INTERFACE
# =========================
st.markdown('<div class="card">', unsafe_allow_html=True)

query = st.text_input("💬 Ask a question about your dataset")

if st.button("🚀 Analyze Data"):

    if st.session_state.df is None:
        st.error("Please upload dataset first.")
    elif not query:
        st.warning("Enter a question.")
    else:
        with st.spinner("AI is analyzing your dataset..."):
            try:
                result = app_graph.invoke({
                    "messages": [query],
                    "sql_query": "",
                    "sql_result": ""
                })

                report = result["messages"][-1]

                st.success("Analysis Complete ✅")
                st.markdown(report)

                with st.expander("🧾 Generated SQL"):
                    st.code(result["sql_query"], language="sql")

                try:
                    df_result = pd.read_json(io.StringIO(result["sql_result"]))
                    st.dataframe(df_result)

                    generate_graph(df_result)

                except:
                    st.error(result["sql_result"])

            except Exception as e:
                st.error(f"Error: {str(e)}")

st.markdown('</div>', unsafe_allow_html=True)