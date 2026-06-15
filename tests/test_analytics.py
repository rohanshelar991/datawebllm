import io
import zipfile

import pandas as pd

from data_intelligence.analytics import (
    build_dataset_health,
    build_filter_mask,
    build_query_package,
    infer_suggested_questions,
    summarize_result,
)


def test_build_dataset_health_reports_core_metrics() -> None:
    df = pd.DataFrame(
        {
            "customer_id": [1, 1, 2],
            "monthly_charges": [10.0, 10.0, None],
            "status": ["Yes", "Yes", "No"],
        }
    )
    health = build_dataset_health(df)
    assert health["rows"] == 3
    assert health["duplicate_rows"] == 1
    assert health["columns_with_nulls"] == 1
    assert health["quality_score"] < 100


def test_infer_suggested_questions_is_schema_aware() -> None:
    df = pd.DataFrame(
        {
            "gender": ["Male", "Female"],
            "churn": ["Yes", "No"],
            "monthly_charges": [70, 80],
            "tenure": [12, 24],
        }
    )
    suggestions = infer_suggested_questions(df)
    assert any("churn" in item.lower() for item in suggestions)
    assert any("monthly_charges" in item.lower() for item in suggestions)


def test_build_filter_mask_applies_multiple_filters() -> None:
    df = pd.DataFrame(
        {
            "segment": ["A", "B", "A"],
            "score": [10, 20, 30],
            "notes": ["hello", "world", "hello again"],
        }
    )
    mask = build_filter_mask(
        df,
        [
            {"column": "segment", "kind": "categorical_values", "value": ["A"]},
            {"column": "score", "kind": "numeric_range", "value": (5, 15)},
            {"column": "notes", "kind": "text_contains", "value": "hello"},
        ],
    )
    filtered = df.loc[mask]
    assert len(filtered) == 1
    assert filtered.iloc[0]["score"] == 10


def test_build_query_package_contains_expected_artifacts() -> None:
    package = build_query_package(
        {
            "question": "Q",
            "content": "A",
            "sql": "SELECT 1",
            "explanation": "E",
            "meta": "M",
            "df": pd.DataFrame({"value": [1, 2]}),
        }
    )
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        names = set(zf.namelist())
        assert {"summary.json", "query.sql", "result.csv"}.issubset(names)


def test_summarize_result_handles_single_cell() -> None:
    df = pd.DataFrame({"answer": [42]})
    assert summarize_result(df) == "answer: 42"

