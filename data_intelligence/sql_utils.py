import re
from typing import Optional

from sqlglot import exp, parse_one

from .constants import MAX_RESULT_ROWS, TABLE_NAME


def validate_select_only(sql: str) -> exp.Expression:
    parsed = parse_one(sql, read="duckdb")
    illegal = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Attach, exp.Detach)
    if parsed.find(*illegal) is not None:
        raise ValueError("Only SELECT queries are allowed.")
    if not parsed.find(exp.Select):
        raise ValueError("Only SELECT queries are allowed.")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("Multiple statements are not allowed.")
    return parsed


def enforce_table_whitelist(parsed: exp.Expression, sql: str) -> None:
    table_names = {t.name.lower() for t in parsed.find_all(exp.Table)}
    if not table_names:
        raise ValueError("Query must reference the dataset table.")
    allowed = {TABLE_NAME.lower()}
    if not table_names.issubset(allowed):
        raise ValueError(f"Query can only reference table '{TABLE_NAME}'. Found: {sorted(table_names)}")
    if "sqlite_master" in sql.lower() or "information_schema" in sql.lower():
        raise ValueError("System tables are not allowed.")


def explain_missing_columns(error_message: str, available: list[str]) -> Optional[str]:
    match = re.search(r'Referenced column "([^"]+)" not found', error_message)
    if not match:
        return None
    missing = match.group(1)
    preview = ", ".join(available[:30])
    suffix = "" if len(available) <= 30 else ", …"
    return f"Missing column `{missing}`. Available columns: {preview}{suffix}"


def ensure_limit(sql: str, parsed: exp.Expression) -> str:
    select = parsed.find(exp.Select)
    if isinstance(select, exp.Select) and select.args.get("limit") is None:
        return sql.rstrip().rstrip(";") + f" LIMIT {MAX_RESULT_ROWS}"
    return sql
