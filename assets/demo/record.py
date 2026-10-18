"""Record the hopcite demo as an asciicast v2 file, without asciinema or a pty.

    python assets/demo/record.py assets/demo/demo.cast

Each step either prints a comment (the amber lines) or types a command and then runs the real
command through `subprocess` (no shell, no pty), streaming its stdout/stderr into the cast with
the real arrival times. Only the prompt, the typing and the comments are produced here; every
command's output is the command's own. Requires the API from the README on 127.0.0.1:8101.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
COLS, ROWS = 100, 30
AMB, GRN, R = "\033[1;33m", "\033[1;32m", "\033[0m"
TYPE_DELAY = 0.025

Q = "Which company is both a foundry for Broadcom and one of Broadcom's assembly and test contract manufacturers?"
Q3 = (
    "Which companies does NVIDIA name as networking competitors that, in turn, name NVIDIA as a competitor"
    " and also list AMD as a competitor?"
)
STEPS: list[tuple[str, ...]] = [
    ("note", "hopcite: graph + vector RAG over 18 SEC filings. API on 127.0.0.1:8101, local llama3.1:8b."),
    ("note", "router LLM off (GROQ_API_KEY empty): deterministic entity linking, both retrieval paths"),
    ("run", "curl -s 127.0.0.1:8101/readyz", "curl", "-s", "http://127.0.0.1:8101/readyz", "\n"),
    ("note", "a 2-hop question: foundry AND assembly/test partner of Broadcom"),
    ("run", f'python assets/demo/ask.py "{Q}"', PY, "assets/demo/ask.py", Q),
    ("note", "same question, vector-only RAG (same index, answer model and prompt)"),
    ("run", f'python assets/demo/ask.py --mode vector "{Q}"', PY, "assets/demo/ask.py", "--mode", "vector", Q),
    ("note", "a 3-hop question: the answer has to join facts from two different filings"),
    ("run", f'python assets/demo/ask.py "{Q3}"', PY, "assets/demo/ask.py", Q3),
    ("note", "the committed 64-question benchmark"),
    ("run", "python assets/demo/table.py", PY, "assets/demo/table.py"),
]


class Cast:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[tuple[float, str]] = []
        self.clock = 0.0  # virtual time: typing pauses advance it, real output keeps real gaps

    def emit(self, data: str, at: float | None = None) -> None:
        data = data.replace("\r\n", "\n").replace("\n", "\r\n")
        self.events.append((round(self.clock if at is None else at, 4), data))

    def wait(self, s: float) -> None:
        self.clock += s

    def type_line(self, text: str) -> None:
        self.emit(f"{GRN}${R} ")
        for ch in text:
            self.wait(TYPE_DELAY)
            self.emit(ch)
        self.wait(0.3)
        self.emit("\n")

    def run(self, argv: list[str], trailer: str = "") -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        start_real, start_clock = time.monotonic(), self.clock
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        buf = b""
        while chunk := proc.stdout.read1(4096):
            buf += chunk
            try:
                text, buf = buf.decode("utf-8"), b""
            except UnicodeDecodeError:
                continue
            self.emit(text, at=start_clock + (time.monotonic() - start_real))
        proc.wait()
        self.clock = start_clock + (time.monotonic() - start_real)
        if trailer:
            self.emit(trailer)
        if proc.returncode:
            raise SystemExit(f"step failed ({proc.returncode}): {argv}")

    def write(self, path: pathlib.Path) -> None:
        header = {
            "version": 2,
            "width": COLS,
            "height": ROWS,
            "timestamp": int(time.time()),
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
            "title": "hopcite: real session",
        }
        lines = [json.dumps(header)] + [json.dumps([t, "o", d], ensure_ascii=False) for t, d in self.events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "assets" / "demo" / "demo.cast")
    cast = Cast()
    for step in STEPS:
        if step[0] == "note":
            cast.emit(f"{AMB}# {step[1]}{R}\n")
            cast.wait(1.2)
        else:
            shown, *argv = step[1:]
            trailer = ""
            if argv and argv[-1] == "\n":
                argv, trailer = argv[:-1], "\n"
            cast.type_line(shown)
            cast.run(argv, trailer)
            cast.wait(2.5)
    cast.emit(f"{AMB}# every claim cites a chunk id the server retrieved; the model never wrote Cypher{R}\n")
    cast.wait(1.0)
    cast.emit("")
    cast.write(out)
    print(f"wrote {out} ({len(cast.events)} events, {cast.clock:.1f} s)")


if __name__ == "__main__":
    main()
