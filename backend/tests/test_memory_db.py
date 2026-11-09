from __future__ import annotations

from types import SimpleNamespace

from app.memory import db, identity


def _settings(tmp_path):
    return SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )


def _patch(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)


def test_init_schema_creates_tables(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"events", "memories", "persons", "memories_vec"} <= names
        assert db.stats(conn) == {"events": 0, "memories": 0, "persons": 0}
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    db.init_schema()
    assert db.stats()["events"] == 0


def test_person_key_rules() -> None:
    assert identity.person_key_web(12) == "wp:12"
    assert identity.person_key_qq(123456) == "qq:123456"
    assert identity.person_key_qq(123456, bound_user_id=12) == "wp:12"


def test_link_identity_makes_history_shared(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        identity.touch_person(conn, "qq:123456", qq_uin="123456")
        identity.touch_person(conn, "wp:12", wp_user_id=12)
        assert identity.linked_keys(conn, "wp:12") == ["wp:12"]

        identity.link_identity(conn, ["wp:12", "qq:123456"], wp_user_id=12, qq_uin="123456")
        assert set(identity.linked_keys(conn, "wp:12")) == {"wp:12", "qq:123456"}
        assert set(identity.linked_keys(conn, "qq:123456")) == {"wp:12", "qq:123456"}
        conn.commit()
    finally:
        conn.close()


def test_touch_person_updates_last_seen(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        identity.touch_person(conn, "wp:12", display_name="阿东", now="2026-01-01T00:00:00+00:00")
        identity.touch_person(conn, "wp:12", display_name="阿东", now="2026-01-02T00:00:00+00:00")
        row = conn.execute("SELECT * FROM persons WHERE person_key = 'wp:12'").fetchone()
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-01-02T00:00:00+00:00"
    finally:
        conn.close()


def test_init_schema_creates_relationship_tables(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"relationship_state", "relationship_events"} <= names

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
        }
        assert {
            "persona_key", "person_key", "affection", "missing", "security",
            "patience", "valence", "energy", "fatigue", "reply_debt", "updated_at",
        } <= columns

        primary_key = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
            if row["pk"]
        ]
        assert primary_key == ["persona_key", "person_key"]
    finally:
        conn.close()


def test_init_schema_adds_role_to_legacy_persons_table(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS persons")
        conn.execute(
            """
            CREATE TABLE persons (
                person_key TEXT PRIMARY KEY,
                display_name TEXT,
                wp_user_id INTEGER,
                qq_uin TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                note TEXT
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(persons)")
        }
        assert "role" in columns
    finally:
        conn.close()


def test_init_schema_adds_fatigue_to_legacy_relationship_state(
    tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS relationship_state")
        conn.execute(
            """
            CREATE TABLE relationship_state (
                persona_key TEXT NOT NULL,
                person_key TEXT NOT NULL,
                affection REAL NOT NULL DEFAULT 0,
                missing REAL NOT NULL DEFAULT 50,
                security REAL NOT NULL DEFAULT 70,
                patience REAL NOT NULL DEFAULT 70,
                valence REAL NOT NULL DEFAULT 0,
                energy REAL NOT NULL DEFAULT 1,
                reply_debt REAL NOT NULL DEFAULT 0,
                last_interaction_at TEXT,
                last_proactive_at TEXT,
                last_reply_at TEXT,
                attention_confidence REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (persona_key, person_key)
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
        }
        assert "fatigue" in columns
    finally:
        conn.close()


def test_init_schema_adds_conflict_columns_to_legacy_relationship_state(
    tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS relationship_state")
        conn.execute(
            """
            CREATE TABLE relationship_state (
                persona_key TEXT NOT NULL,
                person_key TEXT NOT NULL,
                affection REAL NOT NULL DEFAULT 0,
                missing REAL NOT NULL DEFAULT 50,
                security REAL NOT NULL DEFAULT 70,
                patience REAL NOT NULL DEFAULT 70,
                valence REAL NOT NULL DEFAULT 0,
                energy REAL NOT NULL DEFAULT 1,
                reply_debt REAL NOT NULL DEFAULT 0,
                last_interaction_at TEXT,
                last_proactive_at TEXT,
                last_reply_at TEXT,
                attention_confidence REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (persona_key, person_key)
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_state)")
        }
        assert {"conflict_stage", "stage_since", "scar_level"} <= columns
    finally:
        conn.close()


def test_init_schema_creates_appraisal_tables(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"appraisal_usage", "appraisal_log"} <= tables

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(appraisal_log)")
        }
        assert {
            "persona_key", "person_key", "channel_id", "message_id", "event",
            "intensity", "confidence", "reply_bias", "reason", "raw_output",
            "d_affection", "d_valence", "d_security", "d_patience",
            "applied_affection", "applied_valence", "latency_ms", "status",
            "skipped_reason", "created_at",
        } <= columns
    finally:
        conn.close()


def test_init_schema_adds_appraisal_columns_to_legacy_decision_log(
    tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        conn.execute("DROP TABLE IF EXISTS decision_log")
        conn.execute(
            """
            CREATE TABLE decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_key TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                person_key TEXT NOT NULL,
                message_id TEXT,
                rule_score REAL NOT NULL DEFAULT 0,
                willingness REAL NOT NULL DEFAULT 0,
                valence REAL NOT NULL DEFAULT 0,
                energy REAL NOT NULL DEFAULT 1,
                affinity REAL NOT NULL DEFAULT 1,
                reply_debt REAL NOT NULL DEFAULT 0,
                p0 REAL NOT NULL DEFAULT 1,
                p REAL NOT NULL DEFAULT 1,
                roll REAL,
                verdict TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

        db.init_schema(conn)

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(decision_log)")
        }
        assert {
            "reply_bias",
            "appraisal_event",
            "appraisal_confidence",
            "conflict_stage",
        } <= columns
    finally:
        conn.close()


def test_init_schema_creates_decision_log(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, tmp_path)
    db.init_schema()
    conn = db.connect()
    try:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(decision_log)")
        }
        assert {
            "persona_key",
            "channel_id",
            "person_key",
            "message_id",
            "rule_score",
            "willingness",
            "valence",
            "energy",
            "affinity",
            "reply_debt",
            "p0",
            "p",
            "roll",
            "verdict",
            "reason",
            "created_at",
        } <= columns
        # 健康检查的键不能因为新表变化。
        assert set(db.stats(conn)) == {"events", "memories", "persons"}
    finally:
        conn.close()
