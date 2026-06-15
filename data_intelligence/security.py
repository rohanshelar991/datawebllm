import hmac


def parse_timeout_minutes(raw_value: str | None, default: int = 30) -> int:
    try:
        parsed = int(str(raw_value or "").strip())
        return parsed if parsed > 0 else default
    except Exception:
        return default


def verify_password(candidate: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(candidate or "", expected or "")


def is_session_expired(last_active_at: float | None, timeout_minutes: int, now_ts: float) -> bool:
    if not last_active_at:
        return True
    timeout_seconds = max(timeout_minutes, 1) * 60
    return (now_ts - last_active_at) > timeout_seconds
