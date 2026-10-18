# Demo recording

`demo.gif` (40 s, 100×30, ~0.7 MB) is a real session on Windows 11 against the local stack
(Neo4j + pgvector from `docker compose -p p1-kgrag`, the API from `kgrag-api` on 127.0.0.1:8101).
Nothing in it is mocked.

What was real, and what was cached:

- **Retrieval was live**: entity linking, the Cypher template run against Neo4j, the pgvector
  search, citation validation and the API responses all ran during the recording.
- **The answers came from the on-disk LLM cache** (`data/cache/llm`, keyed by a content hash of
  provider, model, messages and parameters). A minute before recording, the same three questions were
  asked once against live local `llama3.1:8b` (Ollama), which filled the cache; the API logged
  31.0 s (cold model load), 3.2 s and 9.3 s for those calls. In the recording the server reports
  0.11 s, 0.05 s and 0.14 s, because only retrieval ran. Real uncached latency is in the benchmark
  table in the main README (p50 8.0 s hybrid).
- **The router LLM was off**: the API was started with `GROQ_API_KEY=` (empty), so routing used
  the deterministic fallback (entity linking from the catalog + both retrieval paths, template
  `neighbors_any(fallback)`). This avoids the shared Groq quota and costs $0. With a Groq key the
  router may pick a more specific template.
- **The benchmark table is the committed result file**: `table.py` prints
  `results/benchmark_summary.json` (the same file `scripts/make_report.py` renders into the README).
  The benchmark itself was not re-run for the recording.
- The questions are benchmark items q031 (2-hop) and q042 (3-hop); the hybrid answers match the
  committed benchmark rows, and vector-only refuses q031 there as well.

How it was made:

- `record.py` is a small recorder (asciinema does not run on Windows). It prints the amber comments
  and the `$` prompt, "types" each command at 25 ms per character, then runs the real command through
  `subprocess` (no shell, no pty), streaming stdout/stderr into an
  [asciicast v2](https://docs.asciinema.org/manual/asciicast/v2/) file with the real arrival times.
  The commands are shown as `python …`; the recorder runs them with the same interpreter
  (`sys.executable`, the project's `.venv`).
- `ask.py` is a stdlib client for `POST /ask`. It only selects and colours response fields. Graph facts
  are filtered to the ones that name an answer entity, and the line says how many of the total are shown.
  Long claims and citation snippets are truncated with `…` to fit 100 columns.
- [agg](https://github.com/asciinema/agg) 1.5.0 rendered the cast with the brand palette (Ink
  `0a1220` background, Snow `eef2f6` text, Teal `2dd4bf` for graph facts, amber for citations) and
  Consolas; `--idle-time-limit 3` shortens pauses longer than 3 s. `frame.py` (Pillow) adds the window
  chrome: 28 px padding, a 48 px title bar and 20 px rounded corners, transparent outside.

To re-record (the stack, a built graph, Ollama with `llama3.1:8b`):

```sh
docker compose -p p1-kgrag start neo4j postgres
GROQ_API_KEY= uv run kgrag-api            # in another terminal; router LLM off
python assets/demo/record.py assets/demo/demo.cast      # run it twice: the first run fills the LLM cache
agg --theme 0a1220,eef2f6,0a1220,f47067,3fb950,f5c451,58a6ff,bc8cff,2dd4bf,c9d1d9,64748b,ff7b72,56d364,f5c451,79c0ff,d2a8ff,5eead4,eef2f6 \
    --font-family Consolas --font-size 18 --idle-time-limit 3 --last-frame-duration 5 assets/demo/demo.cast raw.gif
uv run --no-project --with pillow python assets/demo/frame.py raw.gif assets/demo/demo.gif "hopcite  —  real session"
```
