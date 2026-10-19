"""scripts/check_migrations.py: the CI guard that keeps schema migrations additive."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_migrations", ROOT / "scripts" / "check_migrations.py")
assert _spec and _spec.loader
cm = importlib.util.module_from_spec(_spec)
sys.modules["check_migrations"] = cm
_spec.loader.exec_module(cm)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        kind = "neo4j" if name.endswith(".cypher") else "postgres"
        (tmp_path / kind).mkdir(parents=True, exist_ok=True)
        (tmp_path / kind / name).write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "body,rule",
    [
        ("DROP TABLE routing_log;", "DROP"),
        ("drop index if exists chunks_doc_idx;", "DROP"),
        ("ALTER TABLE chunks RENAME COLUMN text TO body;", "RENAME"),
        ("ALTER TABLE chunks RENAME TO passages;", "RENAME"),
        ("ALTER TABLE chunks ALTER COLUMN ordinal TYPE bigint;", "ALTER ... TYPE"),
        ("ALTER TABLE chunks\n  ALTER COLUMN ordinal SET DATA TYPE bigint;", "ALTER ... TYPE"),
        ("ALTER TABLE chunks DROP COLUMN sha256;", "DROP"),
        ("TRUNCATE routing_log;", "TRUNCATE"),
        ("DO $$ BEGIN DROP TABLE x; END $$;", "DROP"),
    ],
)
def test_destructive_statements_fail(tmp_path: Path, body: str, rule: str) -> None:
    root = _tree(tmp_path, {"0001_a.sql": "CREATE TABLE a (id int);", "0002_b.sql": body})
    assert cm.main(["--root", str(root)]) == 1
    findings = cm.scan(root / "postgres" / "0002_b.sql", body)
    assert rule in {f.rule for f in findings}


def test_cypher_drop_constraint_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"0001_a.sql": "SELECT 1;", "0002_x.cypher": "DROP CONSTRAINT entity_id IF EXISTS;"})
    assert cm.main(["--root", str(root)]) == 1


@pytest.mark.parametrize(
    "body",
    [
        "CREATE TABLE IF NOT EXISTS t (id int, content_type text);",
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS lang text;",
        "-- we never DROP or RENAME here\nCREATE INDEX IF NOT EXISTS i ON t (id);",
        "COMMENT ON TABLE t IS 'do not drop, rename or truncate';",
        "/* DROP TABLE t; */ CREATE TABLE u (id int);",
        "CREATE TYPE mood AS ENUM ('a', 'b');",
    ],
)
def test_additive_statements_pass(tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"0001_a.sql": body})
    assert cm.main(["--root", str(root)]) == 0


def test_marker_with_reason_allows_a_contract_step(tmp_path: Path) -> None:
    body = (
        "-- migration: allow-destructive column unused since v0.3 (expand/contract step 2)\n"
        "ALTER TABLE t DROP COLUMN x;"
    )
    root = _tree(tmp_path, {"0001_a.sql": body})
    assert cm.main(["--root", str(root)]) == 0


@pytest.mark.parametrize(
    "marker",
    [
        "-- migration: allow-destructive\n",  # no reason
        "SELECT 'migration: allow-destructive because'; \n",  # not a comment line
    ],
)
def test_marker_without_reason_or_outside_a_comment_is_ignored(tmp_path: Path, marker: str) -> None:
    root = _tree(tmp_path, {"0001_a.sql": marker + "DROP TABLE t;"})
    assert cm.main(["--root", str(root)]) == 1


def test_repo_migrations_are_additive() -> None:
    assert cm.main(["--root", str(ROOT / "src" / "kgrag" / "migrations")]) == 0


def test_edited_merged_migration_detected(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    mig = tmp_path / "src" / "kgrag" / "migrations"
    _tree(mig, {"0001_a.sql": "CREATE TABLE a (id int);\n"})
    git("add", ".")
    git("commit", "-q", "-m", "base")
    (mig / "postgres" / "0001_a.sql").write_text("CREATE TABLE a (id bigint);\n", encoding="utf-8")
    _tree(mig, {"0002_b.sql": "CREATE TABLE b (id int);\n"})
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert cm.edited_since("HEAD", cm.migration_files(Path("src/kgrag/migrations"))) == [
            "src/kgrag/migrations/postgres/0001_a.sql"
        ]
        assert cm.main(["--base", "HEAD"]) == 1
        (mig / "postgres" / "0001_a.sql").write_text("CREATE TABLE a (id int);\n", encoding="utf-8")
        assert cm.main(["--base", "HEAD"]) == 0  # new 0002 is fine, 0001 unchanged
    finally:
        os.chdir(cwd)
