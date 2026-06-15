import pytest

from data_intelligence.sql_utils import ensure_limit, enforce_table_whitelist, explain_missing_columns, validate_select_only


def test_validate_select_only_accepts_select() -> None:
    parsed = validate_select_only("SELECT * FROM data_table")
    assert parsed is not None


def test_validate_select_only_rejects_delete() -> None:
    with pytest.raises(ValueError):
        validate_select_only("DELETE FROM data_table")


def test_enforce_table_whitelist_rejects_other_tables() -> None:
    parsed = validate_select_only("SELECT * FROM other_table")
    with pytest.raises(ValueError):
        enforce_table_whitelist(parsed, "SELECT * FROM other_table")


def test_ensure_limit_adds_limit_when_missing() -> None:
    parsed = validate_select_only("SELECT * FROM data_table")
    sql = ensure_limit("SELECT * FROM data_table", parsed)
    assert sql.endswith("LIMIT 500")


def test_explain_missing_columns_formats_message() -> None:
    message = 'Binder Error: Referenced column "revenue" not found in FROM clause!'
    explanation = explain_missing_columns(message, ["sales", "region"])
    assert explanation == "Missing column `revenue`. Available columns: sales, region"
