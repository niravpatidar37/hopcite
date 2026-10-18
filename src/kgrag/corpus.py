"""SEC EDGAR corpus: fetch the latest annual report per company and cut out the Business section.

- Descriptive User-Agent with a contact email is mandatory (SEC fair-access policy). Set SEC_USER_AGENT.
- Rate limited to <= 8 requests/second (SEC allows 10).
- Only https://www.sec.gov and https://data.sec.gov URLs are fetched (SSRF guard).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

DEFAULT_UA = "hopcite research niravpatidar37@users.noreply.github.com"
ALLOWED_HOSTS = {"www.sec.gov", "data.sec.gov"}
MIN_INTERVAL_S = 0.125

# ticker -> annual form. Semiconductor / AI-infrastructure supply chain: designers, foundries,
# equipment makers, EDA vendors, memory, and a server OEM that buys accelerators.
COMPANIES: dict[str, str] = {
    "NVDA": "10-K",
    "AMD": "10-K",
    "INTC": "10-K",
    "MU": "10-K",
    "QCOM": "10-K",
    "AVGO": "10-K",
    "TXN": "10-K",
    "AMAT": "10-K",
    "LRCX": "10-K",
    "KLAC": "10-K",
    "MRVL": "10-K",
    "ADI": "10-K",
    "SMCI": "10-K",
    "SNPS": "10-K",
    "CDNS": "10-K",
    "ON": "10-K",
    "MCHP": "10-K",
    "TSM": "20-F",
}
FILER_NAMES: dict[str, str] = {
    "NVDA": "NVIDIA Corporation",
    "AMD": "Advanced Micro Devices, Inc.",
    "INTC": "Intel Corporation",
    "MU": "Micron Technology, Inc.",
    "QCOM": "QUALCOMM Incorporated",
    "AVGO": "Broadcom Inc.",
    "TXN": "Texas Instruments Incorporated",
    "AMAT": "Applied Materials, Inc.",
    "LRCX": "Lam Research Corporation",
    "KLAC": "KLA Corporation",
    "MRVL": "Marvell Technology, Inc.",
    "ADI": "Analog Devices, Inc.",
    "SMCI": "Super Micro Computer, Inc.",
    "SNPS": "Synopsys, Inc.",
    "CDNS": "Cadence Design Systems, Inc.",
    "ON": "ON Semiconductor Corporation",
    "MCHP": "Microchip Technology Incorporated",
    "TSM": "Taiwan Semiconductor Manufacturing Company Limited",
}

# ASML's 20-F is an integrated annual report without a parseable Item 4 span; it is excluded as a
# document (ASML still appears as an entity through other filers' text).

# Filers whose annual report does not use the standard "Item 1. Business" heading (Intel uses a
# cross-reference index). (anchor, start heading, end heading): the section runs from the first
# start heading after the anchor to the next end heading.
SECTION_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "INTC": ("\nNote Regarding Third-Party Information\n", "\nOverview\n", "\nPeople\n"),
}


class FetchError(RuntimeError):
    pass


@dataclass
class ManifestEntry:
    doc_id: str
    ticker: str
    company: str
    cik: str
    form: str
    filing_date: str
    report_date: str
    accession: str
    url: str
    raw_sha256: str
    section: str
    section_chars: int
    section_sha256: str


class EdgarClient:
    def __init__(self, user_agent: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        ua = user_agent or os.environ.get("SEC_USER_AGENT") or DEFAULT_UA
        self._http = httpx.Client(
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            timeout=60.0,
            follow_redirects=False,
            transport=transport,
        )
        self._last = 0.0

    def get(self, url: str) -> bytes:
        check_url(url)
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            self._last = time.monotonic()
            r = self._http.get(url)
            if r.status_code == 200:
                return r.content
            if r.status_code in (429, 503):
                time.sleep(2**attempt + 1)
                continue
            raise FetchError(f"HTTP {r.status_code} for {url}")
        raise FetchError(f"rate limited: {url}")


def check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.port not in (None, 443):
        raise FetchError(f"refusing to fetch non-SEC URL: {url}")


def html_to_text(raw: bytes) -> str:
    soup = BeautifulSoup(raw, "lxml")  # inline XBRL parses fine as HTML
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    # block-level elements become line breaks so headings stay on their own line
    for tag in soup.find_all(["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        tag.insert_after("\n")
    text = soup.get_text(" ")
    text = html.unescape(text).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SECTION_PATTERNS = {
    "10-K": (r"\n\s*item\s*1\.?\s*[\-–—:]?\s*business\b", r"\n\s*item\s*1a\.?\s*[\-–—:]?\s*risk\s+factors"),
    "20-F": (
        r"\n\s*item\s*4\.?\s*[\-–—:]?\s*information\s+on\s+the\s+company",
        r"\n\s*item\s*4a\.?\s*[\-–—:]?\s*unresolved\s+staff\s+comments",
    ),
}


def extract_section(text: str, form: str) -> str:
    """Return the longest start..end span (skips the table of contents, which is short)."""
    start_pat, end_pat = _SECTION_PATTERNS[form]
    starts = [m.start() for m in re.finditer(start_pat, "\n" + text, re.IGNORECASE)]
    ends = [m.start() for m in re.finditer(end_pat, "\n" + text, re.IGNORECASE)]
    best = ""
    for s in starts:
        nxt = [e for e in ends if e > s]
        if not nxt:
            continue
        span = ("\n" + text)[s : nxt[0]].strip()
        if len(span) > len(best):
            best = span
    return best


def extract_section_override(text: str, anchor: str, start: str, end: str) -> str:
    a = text.find(anchor)
    s = text.find(start, max(a, 0))
    e = text.find(end, s + 1) if s >= 0 else -1
    return text[s:e].strip() if s >= 0 and e > s else ""


def latest_filing(client: EdgarClient, cik: str, form: str) -> dict[str, str]:
    sub = json.loads(client.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))
    recent = sub["filings"]["recent"]
    for i, f in enumerate(recent["form"]):
        if f == form:
            return {
                "company": sub.get("name", ""),
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": recent["reportDate"][i],
                "primary": recent["primaryDocument"][i],
            }
    raise FetchError(f"no {form} for CIK {cik}")


def fetch_corpus(out_dir: Path, tickers: dict[str, str] | None = None) -> list[ManifestEntry]:
    tickers = tickers or COMPANIES
    client = EdgarClient()
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tmap = json.loads(client.get("https://www.sec.gov/files/company_tickers.json"))
    by_ticker = {v["ticker"].upper(): str(v["cik_str"]) for v in tmap.values()}
    entries: list[ManifestEntry] = []
    for ticker, form in tickers.items():
        cik = by_ticker.get(ticker)
        if cik is None:
            print(f"skip {ticker}: not in company_tickers.json")
            continue
        try:
            f = latest_filing(client, cik, form)
        except FetchError as exc:
            print(f"skip {ticker}: {exc}")
            continue
        acc = f["accession"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{f['primary']}"
        raw_path = raw_dir / f"{ticker}_{form}.html"
        if raw_path.is_file():
            raw = raw_path.read_bytes()
        else:
            raw = client.get(url)
            raw_path.write_bytes(raw)
        text = html_to_text(raw)
        if ticker in SECTION_OVERRIDES:
            section = extract_section_override(text, *SECTION_OVERRIDES[ticker])
        else:
            section = extract_section(text, form)
        if len(section) < 2000:
            print(f"skip {ticker}: business section not found ({len(section)} chars)")
            continue
        doc_id = f"{ticker}-{form}-{f['report_date'][:4]}"
        (raw_dir / f"{doc_id}.txt").write_text(section, encoding="utf-8")
        entries.append(
            ManifestEntry(
                doc_id=doc_id,
                ticker=ticker,
                company=f["company"],
                cik=cik,
                form=form,
                filing_date=f["filing_date"],
                report_date=f["report_date"],
                accession=f["accession"],
                url=url,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                section="Item 1. Business" if form == "10-K" else "Item 4. Information on the Company",
                section_chars=len(section),
                section_sha256=hashlib.sha256(section.encode()).hexdigest(),
            )
        )
        print(f"ok {doc_id}: {len(section)} chars")
    return entries


def write_manifest(entries: list[ManifestEntry], path: Path) -> None:
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> list[ManifestEntry]:
    return [ManifestEntry(**e) for e in json.loads(path.read_text(encoding="utf-8"))]


def load_section(data_dir: Path, entry: ManifestEntry) -> str:
    return (data_dir / "raw" / f"{entry.doc_id}.txt").read_text(encoding="utf-8")
