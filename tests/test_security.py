from data_intelligence.security import is_session_expired, parse_timeout_minutes, verify_password


def test_parse_timeout_minutes_uses_default_for_invalid_values() -> None:
    assert parse_timeout_minutes("", default=30) == 30
    assert parse_timeout_minutes("0", default=30) == 30
    assert parse_timeout_minutes("bad", default=30) == 30


def test_parse_timeout_minutes_accepts_positive_values() -> None:
    assert parse_timeout_minutes("45", default=30) == 45


def test_verify_password_checks_exact_match() -> None:
    assert verify_password("secret", "secret") is True
    assert verify_password("secret", "different") is False


def test_is_session_expired_handles_missing_timestamp() -> None:
    assert is_session_expired(None, 30, now_ts=1000.0) is True


def test_is_session_expired_respects_timeout() -> None:
    assert is_session_expired(1000.0, 30, now_ts=1100.0) is False
    assert is_session_expired(1000.0, 30, now_ts=3001.0) is True
