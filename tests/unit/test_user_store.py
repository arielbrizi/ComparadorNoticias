from __future__ import annotations

import pytest

from app.user_store import (
    count_users,
    get_user_by_email,
    get_user_by_id,
    init_users_table,
    list_users,
    upsert_user,
)


@pytest.fixture(autouse=True)
def _setup_db(temp_db, monkeypatch):
    monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
    init_users_table()


class TestUpsertUser:
    def test_creates_new_user(self):
        user = upsert_user("alice@test.com", "Alice", "https://img/alice.jpg")
        assert user["email"] == "alice@test.com"
        assert user["name"] == "Alice"
        assert user["picture"] == "https://img/alice.jpg"
        assert user["role"] == "user"
        assert user["id"]
        assert user["created_at"]

    def test_updates_existing_user_on_second_call(self):
        u1 = upsert_user("bob@test.com", "Bob", "")
        u2 = upsert_user("bob@test.com", "Bob Updated", "https://img/bob.jpg")
        assert u1["id"] == u2["id"]
        assert u2["name"] == "Bob Updated"
        assert u2["picture"] == "https://img/bob.jpg"
        assert u2["last_login_at"] >= u1["last_login_at"]

    def test_preserves_name_if_not_provided(self):
        upsert_user("carol@test.com", "Carol", "")
        u2 = upsert_user("carol@test.com", "", "")
        assert u2["name"] == "Carol"

    def test_admin_role_for_admin_email(self):
        user = upsert_user("admin@test.com", "Admin", "")
        assert user["role"] == "admin"

    def test_regular_role_for_non_admin(self):
        user = upsert_user("regular@test.com", "Regular", "")
        assert user["role"] == "user"

    def test_admin_email_case_insensitive(self):
        user = upsert_user("ADMIN@test.com", "Admin Upper", "")
        assert user["role"] == "admin"


class TestGetUser:
    def test_get_by_id(self):
        created = upsert_user("dave@test.com", "Dave", "")
        found = get_user_by_id(created["id"])
        assert found["email"] == "dave@test.com"

    def test_get_by_id_not_found(self):
        assert get_user_by_id("nonexistent") is None

    def test_get_by_email(self):
        upsert_user("eve@test.com", "Eve", "")
        found = get_user_by_email("eve@test.com")
        assert found["name"] == "Eve"

    def test_get_by_email_not_found(self):
        assert get_user_by_email("nobody@test.com") is None


class TestListAndCount:
    def test_list_users_returns_all(self):
        upsert_user("u1@test.com", "U1", "")
        upsert_user("u2@test.com", "U2", "")
        users = list_users()
        assert len(users) >= 2

    def test_list_users_respects_limit(self):
        for i in range(5):
            upsert_user(f"limit{i}@test.com", f"L{i}", "")
        users = list_users(limit=3)
        assert len(users) == 3

    def test_count_users(self):
        initial = count_users()
        upsert_user("count@test.com", "Count", "")
        assert count_users() == initial + 1
