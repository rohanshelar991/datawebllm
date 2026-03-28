import io
import os
import re
import sys
from typing import Optional

import duckdb
import pandas as pd
import requests


DEFAULT_SAMPLE_URL = "https://github.com/Geo-y20/Telco-Customer-Churn-/blob/main/Telco%20Customer%20Churn.csv"


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"\s+", "", u)
    if u.startswith("https:/") and not u.startswith("https://"):
        u = "https://" + u[len("https:/") :]
    if u.startswith("http:/") and not u.startswith("http://"):
        u = "http://" + u[len("http:/") :]
    return u


def github_blob_to_raw(url: str) -> str:
    url = normalize_url(url)
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$", url.strip())
    if not m:
        return url.strip()
    owner, repo, branch, path = m.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


def download_csv(url: str) -> pd.DataFrame:
    raw_url = github_blob_to_raw(url)
    resp = requests.get(raw_url, timeout=30)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text))


def main(sample_url: Optional[str] = None) -> int:
    sample_url = sample_url or os.getenv("SAMPLE_URL") or DEFAULT_SAMPLE_URL
    df = download_csv(sample_url)

    conn = duckdb.connect(database=":memory:")
    conn.register("df_view", df)
    conn.execute("CREATE OR REPLACE TABLE data_table AS SELECT * FROM df_view")

    row_count = conn.execute("SELECT COUNT(*) FROM data_table").fetchone()[0]
    col_count = conn.execute("SELECT COUNT(*) FROM pragma_table_info('data_table')").fetchone()[0]

    print(f"Loaded dataset: {row_count:,} rows × {col_count:,} columns")
    print("First 10 columns:", ", ".join(df.columns[:10]))

    # Pure DuckDB sanity queries (no LLM involved).
    print("\nSanity query: churn distribution")
    churn_dist = conn.execute(
        """
        SELECT Churn, COUNT(*) AS n
        FROM data_table
        GROUP BY Churn
        ORDER BY n DESC
        """.strip()
    ).df()
    print(churn_dist.to_string(index=False))

    if "gender" in df.columns and "Churn" in df.columns:
        print("\nSanity query: churn rate by gender (counts only)")
        churn_by_gender = conn.execute(
            """
            SELECT gender, Churn, COUNT(*) AS n
            FROM data_table
            GROUP BY gender, Churn
            ORDER BY gender, Churn
            """.strip()
        ).df()
        print(churn_by_gender.to_string(index=False))

    print("\nOK")
    return 0


if __name__ == "__main__":
    url_arg = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(main(url_arg))

