import io
import re
from typing import Any, Dict, Optional, Tuple

import pandas as pd


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"\s+", "", u)
    if u.startswith("https:/") and not u.startswith("https://"):
        u = "https://" + u[len("https:/") :]
    if u.startswith("http:/") and not u.startswith("http://"):
        u = "http://" + u[len("http:/") :]
    return u


def github_blob_to_raw(url: str) -> str:
    normalized = normalize_url(url)
    match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$", normalized)
    if not match:
        return normalized
    owner, repo, branch, path = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"


def make_safe_identifier(name: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip()).strip("_").lower()
    if not safe:
        safe = "col"
    if safe[0].isdigit():
        safe = f"c_{safe}"
    return safe


def make_safe_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    mapping: Dict[str, str] = {}
    used: Dict[str, int] = {}
    new_cols = []

    for col in df.columns:
        base = make_safe_identifier(str(col))
        count = used.get(base, 0)
        used[base] = count + 1
        safe = base if count == 0 else f"{base}_{count+1}"
        mapping[str(col)] = safe
        new_cols.append(safe)

    df2 = df.copy()
    df2.columns = new_cols
    return df2, mapping


def parse_dataset_content(name: str, content: str) -> pd.DataFrame:
    lower_name = (name or "").lower()
    if lower_name.endswith(".json"):
        return pd.read_json(io.StringIO(content))
    try:
        return pd.read_csv(io.StringIO(content))
    except Exception:
        return pd.read_json(io.StringIO(content))


def load_dataset(file: Any = None, url: Optional[str] = None, remote_text: Optional[str] = None) -> pd.DataFrame:
    if url and url.strip():
        if remote_text is None:
            raise ValueError("remote_text is required when loading from a URL.")
        return parse_dataset_content(github_blob_to_raw(url), remote_text)

    if file is None:
        raise ValueError("No dataset provided")

    name = getattr(file, "name", "") or ""
    if name.lower().endswith(".csv"):
        return pd.read_csv(file)
    if name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(file)
    if name.lower().endswith(".json"):
        return pd.read_json(file)
    raise ValueError("Unsupported file type. Use CSV, XLSX, or JSON.")

