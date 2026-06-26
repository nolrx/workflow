"""
Unit tests for the deploy-time schema reconcile (middleware_service).

These cover the deterministic, DB-free surface of ``reconcile_schema``:

  * ``_contract_text_target`` — the classifier that decides whether a contract
    column type is an unbounded-text widen target, a bounded ``VARCHAR(n)`` widen
    target, or non-textual (no widen). This is the brain of the WIDEN decision
    that turns a narrow ``VARCHAR(50)`` into ``TEXT`` (the "字段内容过长" fix).
  * the no-op guards — sqlite / no-db / empty-db_schema must short-circuit WITHOUT
    attempting a connection, so a dev (sqlite) deploy and a schema-less contract
    never error.

The actual ADD/WIDEN DDL needs a live postgres and is covered by the deploy
integration smoke (see docs / handoff), not here.
"""
from backend.services.code import middleware_service


def test_contract_text_target_classifies_widen_kinds():
    f = middleware_service._contract_text_target
    # Unbounded text targets → widen a bounded varchar to TEXT.
    assert f("TEXT") == ("text", None)
    assert f("text") == ("text", None)
    assert f("CITEXT") == ("text", None)
    assert f("VARCHAR") == ("text", None)            # length-less varchar ~ text
    assert f("character varying") == ("text", None)
    # Bounded varchar targets → widen only to a LARGER bound.
    assert f("VARCHAR(50)") == ("varchar", 50)
    assert f("varchar( 255 )") == ("varchar", 255)
    assert f("CHARACTER VARYING(100)") == ("varchar", 100)
    # Non-textual types → never widen.
    assert f("INTEGER") == (None, None)
    assert f("TIMESTAMPTZ") == (None, None)
    assert f("BOOLEAN") == (None, None)
    assert f("NUMERIC(10,2)") == (None, None)


def test_widen_target_is_widen_only_never_narrows():
    f = middleware_service._widen_target
    # Bounded varchar narrower than the contract → widen.
    assert f("character varying", 50, "TEXT") == "TEXT"
    assert f("character varying", 50, "VARCHAR(255)") == "VARCHAR(255)"
    assert f("character varying", 100, "VARCHAR(255)") == "VARCHAR(255)"
    # NEVER narrows: an unbounded live varchar must not be narrowed to VARCHAR(n)
    # (this was the F2 bug — `live_len is None` previously took the widen branch).
    assert f("character varying", None, "VARCHAR(255)") is None
    # Already wide enough / equal → no-op.
    assert f("character varying", 255, "VARCHAR(50)") is None
    assert f("character varying", 255, "VARCHAR(255)") is None
    # Unbounded varchar vs TEXT target → already unbounded, no needless rewrite.
    assert f("character varying", None, "TEXT") is None
    # text / non-text live columns are never touched.
    assert f("text", None, "TEXT") is None
    assert f("integer", None, "TEXT") is None
    # Non-textual contract target → never widen.
    assert f("character varying", 50, "INTEGER") is None


def test_allowed_type_re_rejects_ddl_injection_fragments():
    ok = middleware_service._ALLOWED_TYPE_RE
    # Bare column types pass (case-insensitive, optional size/precision).
    for good in ["TEXT", "text", "VARCHAR(36)", "varchar( 255 )", "CHARACTER VARYING(100)",
                 "TIMESTAMPTZ", "timestamp with time zone", "INTEGER", "BIGINT",
                 "NUMERIC(10,2)", "BOOLEAN", "JSONB", "uuid", "double precision"]:
        assert ok.match(good), f"should accept bare type {good!r}"
    # Anything carrying a constraint/default/clause is rejected → that column is
    # skipped rather than emitting non-intended DDL via ADD COLUMN.
    for bad in ["TEXT REFERENCES secret_audit (id)", "TEXT DEFAULT now()",
                "TEXT CHECK (length(x) > 0)", "TEXT GENERATED ALWAYS AS IDENTITY",
                "TEXT COLLATE \"C\"", "TEXT NOT NULL", "TEXT; DROP TABLE x",
                "TEXT)", "'; DROP TABLE x; --", "varchar(36) UNIQUE"]:
        assert not ok.match(bad), f"should reject DDL fragment {bad!r}"


def test_reconcile_noop_on_sqlite_and_none():
    db_schema = {"tables": [{"name": "users", "columns": [{"name": "id", "type": "VARCHAR(36)"}]}]}
    ok, log = middleware_service.reconcile_schema("sqlite:////app/data/app.db", db_schema)
    assert ok is True
    assert "no reconcile" in log
    ok, log = middleware_service.reconcile_schema(None, db_schema)
    assert ok is True
    assert "no reconcile" in log


def test_reconcile_noop_on_empty_schema_without_connecting():
    # A postgres URL but an empty db_schema must short-circuit BEFORE connecting
    # (no engine is created → an unreachable host does not raise here).
    ok, log = middleware_service.reconcile_schema(
        "postgresql://u:p@unreachable-host:5432/db", {"tables": []}
    )
    assert ok is True
    assert "empty db_schema" in log
    ok, log = middleware_service.reconcile_schema(
        "postgresql://u:p@unreachable-host:5432/db", {}
    )
    assert ok is True
    assert "empty db_schema" in log


def test_count_tables_returns_none_for_sqlite_and_none_without_connecting():
    # The empty-schema guard only acts on an explicit 0; sqlite-local / no-db
    # must short-circuit to None (unknown → do not act) WITHOUT connecting.
    assert middleware_service.count_tables(None) is None
    assert middleware_service.count_tables("sqlite:////app/data/app.db") is None

