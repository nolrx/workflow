"""Unit tests for the docs/AGENTS.md scaffold injection (P2-E)."""
from backend.services.code import scaffold


def test_frontend_scaffold_adds_agents_and_arch():
    add = scaffold.ensure_scaffold({"src/main.tsx": b"x"}, kind="frontend")
    assert "AGENTS.md" in add and "docs/ARCHITECTURE.md" in add
    assert isinstance(add["AGENTS.md"], bytes)
    # golden principles encoded in-repo
    assert b"HashRouter" in add["AGENTS.md"]


def test_frontend_scaffold_adds_contract_doc_when_present():
    add = scaffold.ensure_scaffold({}, kind="frontend", contract_block="openapi: 3.1")
    assert "docs/contract.md" in add and b"openapi: 3.1" in add["docs/contract.md"]


def test_frontend_scaffold_no_contract_doc_without_contract():
    add = scaffold.ensure_scaffold({"a": b"1"}, kind="frontend")
    assert "docs/contract.md" not in add


def test_backend_scaffold_adds_agents_not_arch():
    # The backend prompt already mandates a root ARCHITECTURE.md — don't duplicate.
    add = scaffold.ensure_scaffold({"app.py": b"x"}, kind="backend")
    assert "AGENTS.md" in add
    assert "docs/ARCHITECTURE.md" not in add
    assert b"/api" in add["AGENTS.md"]  # the no-/api-prefix golden principle


def test_backend_scaffold_adds_db_schema_doc():
    add = scaffold.ensure_scaffold(
        {}, kind="backend", contract_block="c", db_schema_block="users(id TEXT)"
    )
    assert "docs/db-schema.md" in add and b"users(id TEXT)" in add["docs/db-schema.md"]


def test_scaffold_never_clobbers_agent_files():
    files = {"AGENTS.md": b"agent wrote this", "docs/ARCHITECTURE.md": b"mine"}
    add = scaffold.ensure_scaffold(files, kind="frontend", contract_block="x")
    assert "AGENTS.md" not in add
    assert "docs/ARCHITECTURE.md" not in add
    # but a doc the agent didn't write is still added
    assert "docs/contract.md" in add
