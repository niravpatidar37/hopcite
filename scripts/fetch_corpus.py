"""Fetch the EDGAR corpus and write data/manifest.json.

Usage: SEC_USER_AGENT="your-app you@example.com" uv run python scripts/fetch_corpus.py
"""

from __future__ import annotations

from kgrag.config import REPO_ROOT, load_env
from kgrag.corpus import fetch_corpus, write_manifest


def main() -> None:
    load_env()
    data = REPO_ROOT / "data"
    entries = fetch_corpus(data)
    write_manifest(entries, data / "manifest.json")
    print(f"{len(entries)} documents -> data/manifest.json")


if __name__ == "__main__":
    main()
