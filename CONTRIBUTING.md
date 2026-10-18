# Contributing

Issues and pull requests are welcome. For security problems, see [SECURITY.md](SECURITY.md).

## Development

```sh
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest            # no services or secrets needed; stack tests skip
```

Integration tests need the local stack (`docker compose -p p1-kgrag up -d neo4j postgres`):
`KGRAG_INTEGRATION=1 uv run pytest tests/test_integration.py tests/test_migrations_integration.py`.

## Rules

- **The model never writes Cypher.** New graph question shapes go in as constant, parameterized
  templates in `graph_store.py` plus an enum value in the router schema, with a test.
- **Security controls need tests that fail when the control is removed.** Add a mutant to
  `scripts/mutation_check.py` for any new control.
- **No invented numbers.** Every number in the README comes from a committed file in `results/`.
  If you change the pipeline, re-run the benchmark (`scripts/run_benchmark.py`) and commit the results.
- Never commit secrets, `.env` files, raw filings (`data/raw/`), caches or model weights.
- Small commits with conventional messages (`feat:`, `fix:`, `docs:`, `test:`, `ci:`).
