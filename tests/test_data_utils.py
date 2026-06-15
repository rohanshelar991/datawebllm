import io

import pandas as pd

from data_intelligence.data_utils import (
    github_blob_to_raw,
    load_dataset,
    make_safe_columns,
    normalize_url,
)


def test_normalize_url_fixes_whitespace_and_scheme() -> None:
    raw = "https:/ /github.com/example/repo/blob/main/file.csv"
    assert normalize_url(raw) == "https://github.com/example/repo/blob/main/file.csv"


def test_github_blob_to_raw_converts_blob_url() -> None:
    url = "https://github.com/example/repo/blob/main/path/file.csv"
    assert github_blob_to_raw(url) == "https://raw.githubusercontent.com/example/repo/main/path/file.csv"


def test_make_safe_columns_handles_duplicates_and_numbers() -> None:
    df = pd.DataFrame([[1, 2, 3]], columns=["Monthly Charges", "Monthly Charges", "123Name"])
    safe_df, mapping = make_safe_columns(df)
    assert list(safe_df.columns) == ["monthly_charges", "monthly_charges_2", "c_123name"]
    assert mapping["Monthly Charges"] == "monthly_charges_2"


def test_load_dataset_from_remote_text_csv() -> None:
    csv_text = "A,B\n1,2\n3,4\n"
    loaded = load_dataset(url="https://example.com/data.csv", remote_text=csv_text)
    assert list(loaded.columns) == ["A", "B"]
    assert len(loaded) == 2


def test_load_dataset_from_uploaded_csv() -> None:
    file_obj = io.StringIO("A,B\n5,6\n")
    file_obj.name = "sample.csv"
    loaded = load_dataset(file=file_obj)
    assert loaded.iloc[0].to_dict() == {"A": 5, "B": 6}

