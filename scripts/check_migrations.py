#!/usr/bin/env python3
"""CI guard: schema migrations must stay additive (expand-only).

    python scripts/check_migrations.py                      # scan every versioned migration
    python scripts/check_migrations.py --base origin/main   # also refuse edits to already-merged files

Why: CD runs `python -m kgrag.migrate up` BEFORE the new revision takes traffic, while the previous
revision is still serving (and is the rollback target). A migration that drops, renames or retypes
something breaks the old revision mid-deploy and makes rollback unsafe.

Fails (exit 1) when a versioned migration (NNNN_*.sql / NNNN_*.cypher) or a repeatable R__*.sql
contains, outside comments and string literals:
  DROP ...  |  ... RENAME ...  |  ALTER ... TYPE (ALTER COLUMN ... TYPE / SET DATA TYPE)  |  TRUNCATE
unless the file carries an explicit, reasoned opt-in marker on its own comment line:
  -- migration: allow-destructive <reason>       (SQL)
  // migration: allow-destructive <reason>       (Cypher)
Use it for the CONTRACT step of an expand/contract change, after no deployed revision uses the old
shape. With --base, a versioned file that already exists at <base> must be byte-identical (applied
migrations are immutable; kgrag.migrate also refuses them at run time as checksum drift).
Stdlib only; run from the repository root.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS = Path("src/kgrag/migrations")
VERSIONED = re.compile(r"^\d{4}_[a-z0-9_]+\.(sql|cypher)$")
MARKER = re.compile(r"^[ \t]*(--|//)[ \t]*migration:[ \t]*allow-destructive[ \t]+\S[^\n]{2,}$", re.I | re.M)
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("DROP", re.compile(r"\bDROP\b", re.IGNORECASE)),
    ("RENAME", re.compile(r"\bRENAME\b", re.IGNORECASE)),
    ("ALTER ... TYPE", re.compile(r"\bALTER\b[^;]*?\b(SET\s+DATA\s+)?TYPE\b", re.IGNORECASE | re.DOTALL)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.IGNORECASE)),
]


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.text}"


def strip_comments_and_strings(body: str) -> str:
    """Blank out comments and quoted literals (keeping newlines so line numbers survive)."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        two = body[i : i + 2]
        ch = body[i]
        if two in ("--", "//"):
            j = body.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif two == "/*":
            j = body.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", body[i:j]))
            i = j
        elif ch in ("'", '"', "`"):
            j = i + 1
            while j < n and body[j] != ch:
                j += 2 if body[j] == "\\" else 1
            j = min(n, j + 1)
            out.append(re.sub(r"[^\n]", " ", body[i:j]))
            i = j
        elif two == "$$":  # PL/pgSQL body: scan it as code (DROP inside a DO block is still DDL)
            out.append("  ")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def scan(path: Path, body: str) -> list[Finding]:
    if MARKER.search(body):
        return []
    code = strip_comments_and_strings(body)
    findings = []
    for rule, rx in RULES:
        for m in rx.finditer(code):
            line = code.count("\n", 0, m.start()) + 1
            findings.append(Finding(path.as_posix(), rule, line, body.splitlines()[line - 1].strip()[:120]))
    return sorted(findings, key=lambda f: (f.line, f.rule))


def migration_files(root: Path) -> list[Path]:
    files = []
    for kind in ("postgres", "neo4j"):
        d = root / kind
        if d.is_dir():
            files += [f for f in sorted(d.iterdir()) if VERSIONED.match(f.name) or f.name.startswith("R__")]
    return files


def edited_since(base: str, files: list[Path]) -> list[str]:
    """Versioned files that exist at `base` with different content."""
    out = []
    for f in files:
        if not VERSIONED.match(f.name):
            continue
        rel = f.as_posix()
        try:
            old = subprocess.run(  # fixed argv, no shell
                ["git", "show", f"{base}:{rel}"],
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError:
            continue  # new in this change
        if old.replace(b"\r\n", b"\n") != f.read_bytes().replace(b"\r\n", b"\n"):
            out.append(rel)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail on destructive (non-additive) schema migrations.")
    ap.add_argument("--root", type=Path, default=MIGRATIONS)
    ap.add_argument("--base", help="git ref; versioned migrations present there must be unchanged")
    a = ap.parse_args(argv)
    files = migration_files(a.root)
    if not files:
        print(f"no migrations under {a.root}", file=sys.stderr)
        return 2
    findings = [f for p in files for f in scan(p, p.read_text(encoding="utf-8"))]
    edited = edited_since(a.base, files) if a.base else []
    for f in findings:
        print(f"DESTRUCTIVE {f}")
    for e in edited:
        print(f"EDITED      {e}: applied migrations are immutable; add a new NNNN_*.sql instead")
    if findings or edited:
        print(
            "Migrations must be additive. If this is a deliberate contract step, add a comment line\n"
            "  -- migration: allow-destructive <reason>\nto the file and get it reviewed.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {len(files)} migration files are additive" + (f" and unchanged since {a.base}" if a.base else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
