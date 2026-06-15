from backend.app.auth import hash_password, normalize_email, verify_password


def test_hash_password_roundtrip() -> None:
    hashed = hash_password("super-secret-password")
    assert verify_password("super-secret-password", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email("  Person@Example.COM ") == "person@example.com"
