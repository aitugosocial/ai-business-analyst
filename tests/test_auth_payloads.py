from api.routes.auth.login import build_auth_response_payload
from database.pg_models import User, UserRole


def make_user(role: str, is_admin: bool = False) -> User:
    return User(
        id=1,
        name="Test User",
        email="test@example.com",
        password="hashed",
        confirm_password="hashed",
        role=role,
        is_admin=is_admin,
    )


def test_build_auth_response_payload_preserves_moderator_role_and_is_admin_flag():
    user = make_user(UserRole.MODERATOR.value, is_admin=False)

    payload = build_auth_response_payload(user, access_token="abc123", token_type="bearer")

    assert payload["role"] == UserRole.MODERATOR.value
    assert payload["is_admin"] is False
    assert payload["access_token"] == "abc123"


def test_build_auth_response_payload_falls_back_to_admin_role_for_admin_users():
    user = make_user("legacy_role", is_admin=True)

    payload = build_auth_response_payload(user, access_token="abc123", token_type="bearer")

    assert payload["role"] == "admin"
    assert payload["is_admin"] is True
