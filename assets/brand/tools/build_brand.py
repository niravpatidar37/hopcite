# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools==4.66.1"]
# ///
"""Build every hopcite brand asset from source, reproducibly.

    uv run assets/brand/tools/build_brand.py

Requires `resvg` on PATH for the PNG exports (`cargo install resvg --locked --version 0.48.1`).

The wordmark is set in Space Grotesk (variable font, instanced at Bold / Medium)
and converted to outlines, so the logos render identically without the font
installed. The font is fetched from a pinned google/fonts commit and verified by
SHA-256 (it is not vendored). Space Grotesk is licensed under the SIL Open Font
License 1.1, which permits using its outlines in artwork such as a logo.

The mark: three entity nodes joined by two hop arcs, climbing to the answer
node, which carries a citation ring (the claim is grounded in a retrieved chunk).
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

OUT = pathlib.Path(__file__).resolve().parents[1]
FONT_COMMIT = "00a38a53f92aef923b9353f40128e8f4552ddae4"
FONT_URL = f"https://raw.githubusercontent.com/google/fonts/{FONT_COMMIT}/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf"
FONT_SHA256 = "acad6de1fc93436f5c0f1f4137751ef04f1aea3063e7036535970ffcfbd79f72"

# Palette. One accent (teal), two tones so it passes contrast on both GitHub themes.
INK = "#0A1220"  # mark on light backgrounds; dark tiles
SNOW = "#EEF2F6"  # mark on dark backgrounds
TEAL_ON_LIGHT = "#0F766E"
TEAL_ON_DARK = "#2DD4BF"
MUTED_ON_DARK = "#94A3B8"
GITHUB_LIGHT_BG = "#FFFFFF"
GITHUB_DARK_BG = "#0D1117"


# ----------------------------------------------------------------- contrast


def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ----------------------------------------------------------------- fonts


def load_fonts() -> dict[str, TTFont]:
    cache = OUT / "tools" / ".cache" / f"SpaceGrotesk-{FONT_COMMIT[:12]}.ttf"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_URL, timeout=60) as r:
            cache.write_bytes(r.read())
    data = cache.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != FONT_SHA256:
        cache.unlink()
        sys.exit(f"font checksum mismatch: {digest}")
    return {
        name: instantiateVariableFont(TTFont(io.BytesIO(data)), {"wght": wght})
        for name, wght in (("Bold", 700), ("Medium", 500))
    }


def _ntos(n: float) -> str:
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def text_path(
    font: TTFont, text: str, size: float, x: float, baseline: float, tracking: float = 0.0
) -> tuple[str, float]:
    """Outline `text` as an SVG path. Returns (d, advance width)."""
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs, ntos=_ntos)
    cursor = x
    for ch in text:
        if ord(ch) not in cmap:
            sys.exit(f"glyph missing from font: {ch!r}")
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, baseline)))
        cursor += font["hmtx"][name][0] * scale + tracking
    return pen.getCommands(), cursor - x - tracking


def x_height(font: TTFont, size: float) -> float:
    return font["OS/2"].sxHeight * size / font["head"].unitsPerEm


# ----------------------------------------------------------------- the mark

# Nodes on a 64-unit grid: two hops climbing from the bottom-left to the answer node.
N1, N2, N3 = (13.0, 50.0), (29.0, 33.0), (49.0, 16.0)


def mark(fg: str, accent: str, *, x: float = 0, y: float = 0, size: float = 64, heavy: bool = False) -> str:
    """Three entity nodes, two hop arcs, and a citation ring on the answer node."""
    s = size / 64
    edge = 5.8 if heavy else 4.8
    node = 6.8 if heavy else 6.0
    ring = 4.2 if heavy else 3.6
    (x1, y1), (x2, y2), (x3, y3) = N1, N2, N3
    hops = f"M{x1} {y1}Q{x1 - 1} {y2 - 4} {x2} {y2}M{x2} {y2}Q{x2 + 1} {y3 - 5} {x3} {y3}"
    return (
        f'<g transform="translate({_ntos(x)} {_ntos(y)}) scale({_ntos(s)})">'
        f'<path d="{hops}" fill="none" stroke="{fg}" stroke-width="{edge}" stroke-linecap="round"/>'
        f'<circle cx="{x1}" cy="{y1}" r="{node}" fill="{fg}"/>'
        f'<circle cx="{x2}" cy="{y2}" r="{node}" fill="{fg}"/>'
        f'<circle cx="{x3}" cy="{y3}" r="{node + 4.6}" fill="none" stroke="{accent}" stroke-width="{ring}"/>'
        f'<circle cx="{x3}" cy="{y3}" r="{node - 0.6}" fill="{accent}"/>'
        "</g>"
    )


def tile(size: float, rx: float, pad: float) -> str:
    """Mark on its own ink tile (favicon, avatar). rx=0 gives an opaque full-bleed square."""
    return f'<rect width="{size}" height="{size}" rx="{rx}" fill="{INK}"/>' + mark(
        SNOW, TEAL_ON_DARK, x=pad, y=pad, size=size - 2 * pad, heavy=True
    )


def favicon16() -> str:
    """Hand-hinted 16px variant: straight hops and pixel-centred nodes, so the
    three nodes and the ring stay distinct instead of blurring into one blob."""
    return (
        f'<rect width="16" height="16" rx="3" fill="{INK}"/>'
        f'<path d="M3.5 12.5L7 9L10.5 5.5" fill="none" stroke="{SNOW}" stroke-width="1.4" stroke-linecap="round"/>'
        f'<circle cx="3.5" cy="12.5" r="1.9" fill="{SNOW}"/>'
        f'<circle cx="7" cy="9" r="1.9" fill="{SNOW}"/>'
        f'<circle cx="11" cy="5" r="3.1" fill="none" stroke="{TEAL_ON_DARK}" stroke-width="1.3"/>'
        f'<circle cx="11" cy="5" r="1.2" fill="{TEAL_ON_DARK}"/>'
    )


def svg(width: float, height: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_ntos(width)}" height="{_ntos(height)}" '
        f'viewBox="0 0 {_ntos(width)} {_ntos(height)}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{body}</svg>\n"
    )


def lockup(fonts: dict[str, TTFont], fg: str, accent: str) -> str:
    size, gap, track = 50.0, 14.0, -0.8
    xh = x_height(fonts["Bold"], size)
    baseline = 34 + xh / 2
    d_hop, w_hop = text_path(fonts["Bold"], "hop", size, 64 + gap, baseline, tracking=track)
    d_cite, w_cite = text_path(fonts["Bold"], "cite", size, 64 + gap + w_hop + track, baseline, tracking=track)
    width = 64 + gap + w_hop + track + w_cite + 3
    body = mark(fg, accent) + f'<path fill="{fg}" d="{d_hop}"/><path fill="{accent}" d="{d_cite}"/>'
    return svg(width, 64, body, "hopcite")


def social(fonts: dict[str, TTFont]) -> str:
    W, H, left = 1280, 640, 96
    bold, med = fonts["Bold"], fonts["Medium"]
    parts = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    parts.append(mark(SNOW, TEAL_ON_DARK, x=left - 8, y=84, size=132))
    wsize = 100
    base = 84 + 66 + x_height(bold, wsize) / 2
    d1, w1 = text_path(bold, "hop", wsize, left + 160, base, tracking=-1.6)
    d2, _ = text_path(bold, "cite", wsize, left + 160 + w1 - 1.6, base, tracking=-1.6)
    parts.append(f'<path fill="{SNOW}" d="{d1}"/><path fill="{TEAL_ON_DARK}" d="{d2}"/>')
    for i, line in enumerate(("Multi-hop RAG over SEC filings.", "Every claim carries a validated citation.")):
        d, _ = text_path(med, line, 50, left, 330 + i * 66)
        parts.append(f'<path fill="{SNOW}" d="{d}"/>')
    d, _ = text_path(med, "Graph + vector retrieval. The model never writes Cypher.", 27, left, 452)
    parts.append(f'<path fill="{MUTED_ON_DARK}" d="{d}"/>')
    d, _ = text_path(bold, "Multi-hop accuracy 38% \u2192 63% vs vector-only RAG", 30, left, 506)
    parts.append(f'<path fill="{TEAL_ON_DARK}" d="{d}"/>')
    d, _ = text_path(med, "github.com/niravpatidar37/hopcite", 24, left, 568)
    parts.append(f'<path fill="{MUTED_ON_DARK}" d="{d}"/>')
    return svg(W, H, "".join(parts), "hopcite: multi-hop RAG over SEC filings with validated citations")


# ----------------------------------------------------------------- safety gate

FORBIDDEN = [
    (re.compile(r"<\s*script", re.I), "script element"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "event handler attribute"),
    (re.compile(r"<\s*foreignObject", re.I), "foreignObject"),
    (re.compile(r"(?:xlink:)?href\s*=\s*\"(?!#)", re.I), "external reference"),
    (re.compile(r"url\(\s*['\"]?(?!#)", re.I), "external url()"),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
]


def check_safe(name: str, text: str) -> None:
    for pattern, what in FORBIDDEN:
        if pattern.search(text):
            sys.exit(f"{name}: unsafe SVG content ({what})")


# ----------------------------------------------------------------- main


def main() -> None:
    checks = {
        "accent on GitHub light": contrast(TEAL_ON_LIGHT, GITHUB_LIGHT_BG),
        "accent on GitHub dark": contrast(TEAL_ON_DARK, GITHUB_DARK_BG),
        "ink on GitHub light": contrast(INK, GITHUB_LIGHT_BG),
        "snow on GitHub dark": contrast(SNOW, GITHUB_DARK_BG),
        "accent on ink tile": contrast(TEAL_ON_DARK, INK),
        "muted on ink tile": contrast(MUTED_ON_DARK, INK),
    }
    for label, ratio in checks.items():
        print(f"contrast {label}: {ratio:.2f}:1")
        if ratio < 3.0:
            sys.exit(f"contrast below WCAG 3:1 for graphics: {label}")

    fonts = load_fonts()
    files = {
        "icon.svg": svg(64, 64, mark(INK, TEAL_ON_LIGHT), "hopcite"),
        "icon-dark.svg": svg(64, 64, mark(SNOW, TEAL_ON_DARK), "hopcite"),
        "favicon.svg": svg(32, 32, tile(32, 7, 3), "hopcite"),
        "favicon-16.svg": svg(16, 16, favicon16(), "hopcite"),
        "avatar.svg": svg(512, 512, tile(512, 0, 72), "hopcite"),
        "logo.svg": lockup(fonts, INK, TEAL_ON_LIGHT),
        "logo-dark.svg": lockup(fonts, SNOW, TEAL_ON_DARK),
        "social-preview.svg": social(fonts),
    }
    for name, text in files.items():
        check_safe(name, text)
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {name} ({len(text)} bytes)")

    resvg = shutil.which("resvg")
    if not resvg:
        sys.exit("resvg not found on PATH; install it to export PNGs")
    renders = [
        ("favicon-16.svg", "favicon-16.png", 16),
        ("favicon.svg", "favicon-32.png", 32),
        ("avatar.svg", "avatar-512.png", 512),
        ("social-preview.svg", "social-preview.png", 1280),
    ]
    for src, dst, width in renders:
        # List-form argv, no shell: every argument is a constant from `renders`
        # plus the absolute resvg path from shutil.which.
        argv = [resvg, "-w", str(width), str(OUT / src), str(OUT / dst)]
        subprocess.run(argv, check=True, shell=False)  # nosec B603
        print(f"rendered {dst} ({(OUT / dst).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
