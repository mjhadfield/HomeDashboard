"""Migration atomicity, using only new databases and SQL files under pytest's scratch directory."""
import sqlite3

import pytest

from app import config, db

REAL_MIGRATIONS = db.MIGRATIONS


@pytest.fixture()
def scratch_migrations(tmp_path, monkeypatch):
    data = tmp_path / "data"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "hub.sqlite")
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    return migrations


def test_failed_migration_rolls_back_schema_and_data_and_can_be_retried(scratch_migrations):
    (scratch_migrations / "001_initial.sql").write_text("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO projects VALUES (1, 'original');
    """)
    migration = scratch_migrations / "002_launchpad.sql"
    changes = """
        ALTER TABLE projects ADD COLUMN show_launchpad INTEGER NOT NULL DEFAULT 0;
        UPDATE projects SET name = 'changed';
        ALTER TABLE projects ADD COLUMN icon TEXT NOT NULL DEFAULT '';
    """
    migration.write_text(changes + "INSERT INTO missing_table VALUES (1);")

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        db.migrate()

    # Reopen after failure: the earlier migration remains, but none of the failed one does.
    with db.db() as con:
        assert [r["name"] for r in con.execute("PRAGMA table_info(projects)")] == ["id", "name"]
        assert con.execute("SELECT name FROM projects").fetchone()[0] == "original"
        assert [r["name"] for r in con.execute("SELECT name FROM schema_migrations")] == ["001_initial.sql"]

    migration.write_text(changes)
    assert db.migrate() == ["002_launchpad.sql"]
    assert db.migrate() == []
    with db.db() as con:
        assert tuple(con.execute("SELECT name, show_launchpad, icon FROM projects").fetchone()) == ("changed", 0, "")
        assert con.execute("SELECT COUNT(*) FROM schema_migrations WHERE name = '002_launchpad.sql'").fetchone()[0] == 1


def test_failed_applied_marker_rolls_back_the_migration(scratch_migrations):
    (scratch_migrations / "001_initial.sql").write_text("""
        CREATE TABLE projects (name TEXT);
        INSERT INTO projects VALUES ('original');
        CREATE TRIGGER reject_marker BEFORE INSERT ON schema_migrations
        WHEN NEW.name = '002_change.sql'
        BEGIN
            SELECT RAISE(ABORT, 'marker rejected');
        END;
    """)
    (scratch_migrations / "002_change.sql").write_text("""
        ALTER TABLE projects ADD COLUMN icon TEXT;
        UPDATE projects SET name = 'changed';
    """)

    with pytest.raises(sqlite3.IntegrityError, match="marker rejected"):
        db.migrate()

    with db.db() as con:
        assert [r["name"] for r in con.execute("PRAGMA table_info(projects)")] == ["name"]
        assert con.execute("SELECT name FROM projects").fetchone()[0] == "original"
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        con.execute("DROP TRIGGER reject_marker")
    assert db.migrate() == ["002_change.sql"]
    assert db.migrate() == []


def test_failed_commit_rolls_back_both_changes_and_marker(scratch_migrations):
    (scratch_migrations / "001_initial.sql").write_text("""
        CREATE TABLE parents (id INTEGER PRIMARY KEY);
        CREATE TABLE children (parent_id INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED);
    """)
    (scratch_migrations / "002_change.sql").write_text("""
        ALTER TABLE children ADD COLUMN name TEXT;
        INSERT INTO children (parent_id, name) VALUES (1, 'orphan');
    """)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.migrate()

    with db.db() as con:
        assert [r["name"] for r in con.execute("PRAGMA table_info(children)")] == ["parent_id"]
        assert con.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0
        assert [r["name"] for r in con.execute("SELECT name FROM schema_migrations")] == ["001_initial.sql"]
        con.execute("INSERT INTO parents VALUES (1)")
    assert db.migrate() == ["002_change.sql"]
    assert db.migrate() == []


def test_all_existing_migrations_apply_once_on_a_fresh_scratch_database(scratch_migrations, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", REAL_MIGRATIONS)
    expected = [p.name for p in sorted(REAL_MIGRATIONS.glob("*.sql"))]
    assert expected
    assert db.migrate() == expected
    assert db.migrate() == []
    with db.db() as con:
        assert [r["name"] for r in con.execute("SELECT name FROM schema_migrations ORDER BY name")] == expected
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
