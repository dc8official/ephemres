"""Font metrics, WinAnsi encoding and line wrapping for the native PDF typesetter.

The report PDFs use the standard Helvetica and Helvetica-Bold fonts, which every PDF viewer
provides, so nothing is embedded. Their advance widths (Adobe core-14 metrics, 1/1000 em, for
WinAnsiEncoding codes 32-255) let the typesetter measure text in points: wrap it, size table
columns and right-align numbers. Helvetica digits all share one width, so figures line up.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.pdf_ttf import TrueTypeFont

HELVETICA_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350,
    556, 350, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350,
    350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667,
    278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,
    400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,
    667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
    556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,
)
HELVETICA_BOLD_WIDTHS = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350,
    556, 350, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350,
    350, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350, 500, 667,
    278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333,
    400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611,
    722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
    556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278,
    611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556,
)

FONT_REGULAR = "/F1"
FONT_BOLD = "/F2"
_WIDTHS = {FONT_REGULAR: HELVETICA_WIDTHS, FONT_BOLD: HELVETICA_BOLD_WIDTHS}

# Text Helvetica cannot draw (outside WinAnsi: Greek, Cyrillic, extended Latin, arrows, maths)
# is set in DejaVu Sans, embedded as a subset. These runs hold plain Unicode, not WinAnsi.
FONT_FALLBACK = "/F3"
FONT_FALLBACK_BOLD = "/F4"
_FONT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts"))
_FALLBACK_FILES = {FONT_FALLBACK: "DejaVuSans.ttf", FONT_FALLBACK_BOLD: "DejaVuSans-Bold.ttf"}
_loaded: Dict[str, Optional[TrueTypeFont]] = {}
_load_lock = threading.Lock()


def embedded_font(font: str) -> Optional[TrueTypeFont]:
    """The TrueType font behind an embedded font resource, loaded once; None if unavailable."""
    if font not in _FALLBACK_FILES:
        return None
    if font not in _loaded:
        with _load_lock:
            if font not in _loaded:
                try:
                    _loaded[font] = TrueTypeFont.from_file(os.path.join(_FONT_DIR, _FALLBACK_FILES[font]))
                except (OSError, ValueError):
                    _loaded[font] = None
    return _loaded[font]


def is_embedded(font: str) -> bool:
    return font in _FALLBACK_FILES


_HELVETICA_X_HEIGHT = 523 / 1000.0


def size_factor(font: str) -> float:
    """Scale for an embedded font so its lowercase matches Helvetica's x-height at the same
    nominal size (DejaVu Sans is drawn about 5% smaller); 1.0 for Helvetica."""
    ttf = embedded_font(font) if font in _FALLBACK_FILES else None
    if ttf is None or ttf.x_height <= 0:
        return 1.0
    return max(0.85, min(1.0, _HELVETICA_X_HEIGHT / (ttf.x_height / ttf.units_per_em)))


def shape(text: str, font: str) -> Tuple[str, str]:
    """Choose how to draw text: WinAnsi in Helvetica when it can, otherwise Unicode in the
    embedded fallback font. Characters neither font has become "?" (the tagged PDF still
    carries the original as /ActualText). Returns (string to measure and draw, font)."""
    text = str(text)
    if is_lossless(text):
        return to_winansi(text), font
    fallback = FONT_FALLBACK_BOLD if font == FONT_BOLD else FONT_FALLBACK
    ttf = embedded_font(fallback)
    if ttf is None:
        return to_winansi(text), font
    return "".join(c if (c == " " or ttf.has_char(c)) else "?" for c in text), fallback

# Characters outside WinAnsi that have a readable stand-in. Anything else outside WinAnsi
# prints as "?"; the tagged PDF still carries the original text as /ActualText.
_FALLBACKS = {
    " ": " ", " ": " ", " ": " ", "−": "-", "→": "->", "➔": "->",
    "←": "<-", "≥": ">=", "≤": "<=", "≠": "!=", "≈": "~",
}
_FALLBACK_RE = re.compile("|".join(map(re.escape, _FALLBACKS)))

# Where a word with no spaces (FQDN, IPv6 address, interface name, timestamp) may break.
_SEGMENT_RE = re.compile(r"[^.\-:/_,]*[.\-:/_,]+|[^.\-:/_,]+")


def to_winansi(text: str) -> str:
    """Return text as WinAnsi (cp1252) code points, one str char per byte value."""
    s = _FALLBACK_RE.sub(lambda m: _FALLBACKS[m.group(0)], str(text))
    return s.encode("cp1252", errors="replace").decode("latin-1")


def is_lossless(text: str) -> bool:
    """True when text survives WinAnsi encoding unchanged (no /ActualText needed)."""
    try:
        str(text).encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def text_width(encoded: str, font: str, size: float, tracking: float = 0.0) -> float:
    """Width in points of text as returned by shape(): WinAnsi for Helvetica, Unicode for an
    embedded font."""
    if font in _FALLBACK_FILES:
        ttf = embedded_font(font)
        units = sum(ttf.advance_1000(ttf.glyph(c)) for c in encoded) if ttf else 0.0
        return units * size * size_factor(font) / 1000.0 + tracking * len(encoded)
    widths = _WIDTHS[font]
    units = sum(widths[o - 32] if 32 <= o <= 255 else 0 for o in map(ord, encoded))
    return units * size / 1000.0 + tracking * len(encoded)


def segments(word: str) -> List[str]:
    """Split a word at identifier separators, keeping each separator at the end of its piece."""
    return _SEGMENT_RE.findall(word) or [word]


def min_word_width(encoded: str, font: str, size: float, tracking: float = 0.0, split_words: bool = False) -> float:
    """Widest piece that cannot be broken: a space-separated word, or a segment if split_words."""
    pieces: Sequence[str] = encoded.split(" ")
    if split_words:
        pieces = [seg for word in pieces for seg in segments(word)]
    return max((text_width(p, font, size, tracking) for p in pieces), default=0.0)


def wrap_text(encoded: str, font: str, size: float, width: float, tracking: float = 0.0) -> List[str]:
    """Greedy wrap to width in points. Breaks at spaces, then at identifier separators, then
    between characters. Never drops text."""
    def fits(s: str) -> bool:
        return text_width(s, font, size, tracking) <= width + 0.01

    lines: List[str] = []
    cur = ""
    for word in encoded.split(" "):
        candidate = f"{cur} {word}" if cur else word
        if fits(candidate):
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if fits(word):
            cur = word
            continue
        for piece in segments(word):
            if fits(cur + piece):
                cur += piece
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if fits(piece):
                cur = piece
                continue
            for ch in piece:
                if cur and not fits(cur + ch):
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
    if cur or not lines:
        lines.append(cur)
    return lines
