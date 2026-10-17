"""Paragraph-packing chunker. chunk_id = "<doc_id>#c<NNNN>" is shared by Neo4j and pgvector."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

TARGET_CHARS = 1200
MAX_CHARS = 1800

RELATION_CUES = re.compile(
    r"\b(customer|supplier|suppl(y|ies)|foundr|compet|acqui|subsidiar|partner|collaborat|alliance|"
    r"manufactur|outsourc|headquarter|licens|distributor|sole source|contract)\w*",
    re.IGNORECASE,
)
ORG_HINT = re.compile(
    r"\b[A-Z][A-Za-z0-9&\.\-]+(?:\s+[A-Z][A-Za-z0-9&\.\-]+)*\s+"
    r"(?:Inc|Incorporated|Corporation|Corp|Ltd|Limited|Co|N\.V|plc|LLC|Holdings|Technologies|Technology)\b"
    r"|\b(?:TSMC|Samsung|NVIDIA|AMD|Intel|Qualcomm|Broadcom|Micron|Apple|Microsoft|Amazon|Google|Alphabet|"
    r"Meta|ASML|SK hynix|Huawei|MediaTek|Arm|Marvell|Dell|HPE|Hewlett Packard|Lenovo|Synopsys|Cadence|"
    r"Siemens|Applied Materials|Lam Research|KLA|Tokyo Electron|GlobalFoundries|UMC|Texas Instruments|"
    r"Analog Devices|Infineon|STMicroelectronics|NXP|Renesas|Microchip|onsemi|Supermicro|Foxconn|"
    r"Amkor|ASE|Kioxia|Western Digital|Cisco|Oracle|IBM|Tesla|Xilinx|Altera|Mellanox|VMware)\b"
)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    ordinal: int
    section_path: str
    text: str
    sha256: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sha256:
            self.sha256 = hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _is_heading(line: str) -> bool:
    s = line.strip()
    return 2 < len(s) < 70 and not s.endswith((".", ",", ";", ":")) and s[0].isupper() and len(s.split()) <= 9


_NOISE = re.compile(r"^(Table of Contents|\d+|Page \d+|.*\|\s*\d{4} Form 10-K\s*\|.*)$", re.IGNORECASE)


def chunk_document(doc_id: str, section_title: str, text: str) -> list[Chunk]:
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip() and not _NOISE.match(p.strip())]
    chunks: list[Chunk] = []
    buf: list[str] = []
    heading = section_title
    buf_heading = heading

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if len(body) > 80:
            chunks.append(
                Chunk(f"{doc_id}#c{len(chunks):04d}", doc_id, len(chunks), f"{section_title} > {buf_heading}", body)
            )
        buf = []

    for p in paras:
        if _is_heading(p):
            if sum(len(b) for b in buf) >= TARGET_CHARS // 2:
                flush()
            heading = p
            if not buf:
                buf_heading = heading
            buf.append(p)
            continue
        while len(p) > MAX_CHARS:  # split very long paragraphs on sentence boundaries
            cut = p.rfind(". ", 0, MAX_CHARS)
            cut = cut + 1 if cut > TARGET_CHARS // 2 else MAX_CHARS
            if buf:
                flush()
                buf_heading = heading
            buf.append(p[:cut])
            flush()
            buf_heading = heading
            p = p[cut:].strip()
        if not buf:
            buf_heading = heading
        if sum(len(b) for b in buf) + len(p) > TARGET_CHARS and buf:
            flush()
            buf_heading = heading
        buf.append(p)
    if buf:
        flush()
    return chunks


def relation_score(text: str) -> int:
    """Cheap pre-filter: how likely a chunk states inter-company relations (used to pick chunks for
    paid extraction). Score = distinct relation cue words x distinct organisation mentions."""
    cues = {m.group(0).lower()[:6] for m in RELATION_CUES.finditer(text)}
    orgs = {m.group(0) for m in ORG_HINT.finditer(text)}
    return len(cues) * len(orgs)
