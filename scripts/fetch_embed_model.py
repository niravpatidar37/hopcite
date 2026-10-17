"""Download the embedding model at a pinned revision and verify its hash (Docker build step).

    python scripts/fetch_embed_model.py /app/models/bge-small-en-v1.5

BAAI/bge-small-en-v1.5 is served by fastembed from the ONNX export Qdrant/bge-small-en-v1.5-onnx-Q.
The revision below is the one in the local fastembed cache that built the published index and ran the
benchmark, so the image embeds queries with byte-identical weights.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ID = "Qdrant/bge-small-en-v1.5-onnx-Q"
REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
FILES = ["config.json", "model_optimized.onnx", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"]
ONNX_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"


def main(dest: str) -> int:
    from huggingface_hub import snapshot_download

    out = Path(dest)
    snapshot_download(repo_id=REPO_ID, revision=REVISION, allow_patterns=FILES, local_dir=str(out))
    digest = hashlib.sha256((out / "model_optimized.onnx").read_bytes()).hexdigest()
    if digest != ONNX_SHA256:
        print(f"model hash mismatch: {digest}", file=sys.stderr)
        return 1
    missing = [f for f in FILES if not (out / f).is_file()]
    if missing:
        print(f"missing files: {missing}", file=sys.stderr)
        return 1
    print(f"{REPO_ID}@{REVISION[:12]} -> {out} (sha256 ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "models/bge-small-en-v1.5"))
