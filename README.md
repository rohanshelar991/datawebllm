# Conversational Data Intelligence Platform (PS-2)

Streamlit app that lets non-technical users ask natural-language questions over **any structured CSV/XLSX/JSON** dataset.

Per query, it returns:
- Data-backed natural language answer (derived from the SQL result)
- Executed SQL query
- Brief explanation of derivation
- Optional visualization (when the result shape supports it)
- Result table + CSV download (for the returned rows)

## Setup

1) Install deps
```bash
pip install -r requirements.txt
```

2) Configure API key (Groq)
- Copy `.env.example` to `.env`
- Set `GROQ_API_KEY` in `.env`
  - Alternatively: start the app and set the key in the sidebar (**session only**, not written to disk)
  - For deployment: set `GROQ_API_KEY` as an environment variable or Streamlit secrets (recommended)

3) Run
```bash
streamlit run app.py
```

## Smoke test (no LLM required)
Validates that the sample dataset downloads and DuckDB can query it.
```bash
python3 scripts/smoke_test.py
```

## Deployment (no re-entering keys)
Recommended options (keys are **not** committed to git):

- **Environment variable (most platforms)**
  - Set `GROQ_API_KEY` in your hosting provider’s environment settings.

- **Streamlit secrets (Streamlit Community Cloud / managed)**
  - Locally (optional): copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and set `GROQ_API_KEY`.
  - On Streamlit Cloud: add `GROQ_API_KEY` in the app **Secrets** UI.

### Streamlit Community Cloud (step-by-step)
1) Push this folder as a GitHub repo (or as a subfolder in your repo).
2) On Streamlit Cloud → **New app**:
   - Repository: your GitHub repo
   - Branch: `main`
   - Main file path: `app.py`
3) In the app settings → **Secrets** add:
   - `GROQ_API_KEY="gsk_..."`
4) Deploy.

Notes:
- Streamlit Cloud installs dependencies from `requirements.txt` (this repo includes a `requirements.txt` shim that references `requirement.txt`).
- Do **not** commit `.env` or `.streamlit/secrets.toml` (they are git-ignored).

## Load the provided sample dataset
- Use the sidebar **Load sample** button (preconfigured to the Telco Customer Churn CSV).
- Or paste this GitHub link in the URL box and click **Load dataset**:
  - `https://github.com/Geo-y20/Telco-Customer-Churn-/blob/main/Telco%20Customer%20Churn.csv`

## Notes on grounding / anti-hallucination
- The LLM is used only to generate a **single SELECT** query over the uploaded dataset table.
- The app validates the SQL (SELECT-only + table whitelist) and then generates the answer **only from the returned rows**.

## Extra features
- **Dataset profile** tab: preview, column mapping (original→safe), per-column null counts, top values, and numeric stats.
- **Query metadata**: rows returned + runtime (ms) shown alongside answers.
