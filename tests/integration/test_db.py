import pytest

from app.db import execute, get_conn, query


class TestGetConn:
    def test_creates_sqlite_connection(self, temp_db):
        with get_conn() as conn:
            assert conn is not None
            cur = conn.execute("SELECT 1")
            assert cur.fetchone()[0] == 1

    def test_file_created(self, temp_db):
        with get_conn() as conn:
            conn.execute("SELECT 1")
        assert temp_db.exists()


class TestExecuteAndQuery:
    def test_create_and_insert(self, temp_db):
        with get_conn() as conn:
            execute(conn, "CREATE TABLE test_tbl (id TEXT PRIMARY KEY, val TEXT)")
            execute(conn, "INSERT INTO test_tbl VALUES ('a', 'hello')")

        with get_conn() as conn:
            rows = query(conn, "SELECT * FROM test_tbl").fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == "a"
            assert rows[0]["val"] == "hello"

    def test_parameterized_query(self, temp_db):
        with get_conn() as conn:
            execute(conn, "CREATE TABLE param_test (id INTEGER PRIMARY KEY, name TEXT)")
            execute(conn, "INSERT INTO param_test VALUES (?, ?)", (1, "test"))

        with get_conn() as conn:
            rows = query(conn, "SELECT * FROM param_test WHERE id = ?", (1,)).fetchall()
            assert len(rows) == 1
            assert rows[0]["name"] == "test"


class TestRollback:
    def test_rollback_on_error(self, temp_db):
        with get_conn() as conn:
            execute(conn, "CREATE TABLE rollback_test (id TEXT PRIMARY KEY)")
            execute(conn, "INSERT INTO rollback_test VALUES ('keep')")

        with pytest.raises(Exception):
            with get_conn() as conn:
                execute(conn, "INSERT INTO rollback_test VALUES ('discard')")
                raise RuntimeError("Force rollback")

        with get_conn() as conn:
            rows = query(conn, "SELECT * FROM rollback_test").fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == "keep"
