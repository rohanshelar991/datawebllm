import io
import json
import zipfile
from typing import Any, Dict, List

import pandas as pd


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


def summarize_result(df: pd.DataFrame) -> str:
    if df.empty:
        return "No rows matched the question, so the dataset does not contain enough data to answer it."

    if df.shape == (1, 1):
        col = df.columns[0]
        val = df.iloc[0, 0]
        return f"{col}: {val}"

    if df.shape[0] == 1 and df.shape[1] <= 8:
        parts = [f"- {col}: {df.iloc[0][col]}" for col in df.columns]
        return "Result (single row):\n" + "\n".join(parts)

    if df.shape[1] == 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
        top = df.head(10).copy()
        rows = "\n".join([f"- {top.iloc[i, 0]}: {top.iloc[i, 1]}" for i in range(len(top))])
        suffix = "" if len(df) <= 10 else f"\n(Showing 10 of {len(df)} rows.)"
        return f"Top results:\n{rows}{suffix}"

    return f"Query returned {len(df)} rows × {len(df.columns)} columns. See the table below for exact values."

