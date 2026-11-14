from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import io
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import struct
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote_to_bytes, urlparse
import zlib

from app.core.pdf_fonts import (
    FONT_BOLD,
    FONT_FALLBACK_BOLD,
    FONT_REGULAR,
    embedded_font,
    is_lossless,
    min_word_width,
    segments,
    shape,
    size_factor,
    text_width,
    to_winansi,
    wrap_text,
)
from app.version import VERSION

logger = logging.getLogger(__name__)

ALLOWED_ASSET_DIRS = (
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates", "reports")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "assets")),
)

# ISO 32000-1 Table 20: 32-byte Standard Encryption Padding String
_PDF_PADDING = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41,
    0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
    0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])

# Read-only permissions: PRINT (bit 3 = 4) | EXTRACT_FOR_ACCESSIBILITY (bit 10 = 512)
# | PRINT_TO_REPRESENTATION (bit 12 = 2048) = 2564. Modify, copy, annotate and assemble stay off.
# Bit 10 lets screen readers read the report (PDF/UA-1 7.16 requires it on encrypted files).
# With reserved bits set per ISO 32000-1 Table 22 (0xFFFFF0C0 | 2564 = 0xFFFFFAC4 -> -1340 signed 32-bit)
PDF_READONLY_PERMISSIONS = -1340


class FetchedResource:
    """
    Container returned by secure_url_fetcher supporting both object-attribute
    (.content_type, .read()) and dictionary-style (["mime_type"], ["raw_bytes"]) access.
    """

    def __init__(self, content_type: str, raw_bytes: bytes, url: str = "") -> None:
        self.content_type = content_type
        self.mime_type = content_type
        self.raw_bytes = raw_bytes
        self.url = url
        self._buf = io.BytesIO(raw_bytes)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def seek(self, pos: int) -> int:
        return self._buf.seek(pos)

    def __getitem__(self, key: str) -> Any:
        if key in ("mime_type", "content_type"):
            return self.content_type
        if key in ("raw_bytes", "bytes", "string"):
            return self.raw_bytes
        if key == "file_obj":
            return io.BytesIO(self.raw_bytes)
        raise KeyError(key)


def secure_url_fetcher(url: str, timeout: int = 10, ssl_context: Any = None) -> FetchedResource:
    """
    Air-gapped, SSRF/LFI-hardened URL fetcher for report assets:
    - Permits inline data: URIs (e.g. Base64 logos).
    - Blocks all external network schemas (http, https, ftp, etc.).
    - Restricts local file:// paths strictly to authorized template and asset directories.
    """
    if not url or not isinstance(url, str):
        raise ValueError("Empty URL cannot be fetched.")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "data":
        header_and_data = url[5:]
        if "," not in header_and_data:
            raise ValueError("Malformed data: URI.")
        meta, payload = header_and_data.split(",", 1)
        parts = [p.strip() for p in meta.split(";") if p.strip()]
        is_base64 = "base64" in [p.lower() for p in parts]
        mime_type = parts[0] if (parts and "/" in parts[0]) else "application/octet-stream"
        if is_base64:
            raw_bytes = base64.b64decode(payload)
        else:
            raw_bytes = unquote_to_bytes(payload)
        return FetchedResource(content_type=mime_type, raw_bytes=raw_bytes, url=url)

    if scheme in ("http", "https", "ftp") or (scheme and scheme != "file"):
        logger.warning("SSRF blocked: attempted external fetch of '%s'", url)
        raise PermissionError(f"External network access is prohibited during report compilation: {scheme}://")

    # Handle local filesystem paths
    local_path = parsed.path if scheme == "file" else url
    abs_path = os.path.abspath(local_path)

    is_allowed = any(
        Path(abs_path).resolve().is_relative_to(Path(allowed_dir).resolve())
        for allowed_dir in ALLOWED_ASSET_DIRS
    )
    if not is_allowed or not os.path.isfile(abs_path):
        logger.warning("LFI blocked: attempted access to unauthorized local path '%s'", abs_path)
        raise PermissionError(f"Access to file path '{local_path}' is denied: not within authorized asset roots.")

    mime_guess, _ = mimetypes.guess_type(abs_path)
    with open(abs_path, "rb") as f:
        raw_bytes = f.read()
    return FetchedResource(
        content_type=mime_guess or "application/octet-stream",
        raw_bytes=raw_bytes,
        url=f"file://{abs_path}",
    )


secure_url_fetcher._fail_on_errors = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ISO 32000-1 Standard Security Handler (PDF 1.4 V=2, R=3, 128-bit RC4)
# ---------------------------------------------------------------------------
def _rc4_crypt(key: bytes, data: bytes) -> bytes:
    """Pure-Python RC4 stream cipher for ISO 32000-1 PDF 1.4 encryption/decryption."""
    s = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % key_len]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = 0
    j = 0
    out = bytearray(len(data))
    for idx, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[idx] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _pad_pdf_key(key_material: str) -> bytes:
    raw = key_material.encode("latin-1", errors="ignore")[:32]
    return raw + _PDF_PADDING[: 32 - len(raw)]


def _compute_owner_value_r3(user_key: str, owner_key: str) -> bytes:
    """ISO 32000-1 Algorithm 3: Computing the encryption dictionary's O (owner key) value."""
    padded_owner = _pad_pdf_key(owner_key if owner_key else user_key)
    digest = hashlib.md5(padded_owner, usedforsecurity=False).digest()
    for _ in range(50):
        digest = hashlib.md5(digest, usedforsecurity=False).digest()
    rc4_key = digest[:16]

    data = _pad_pdf_key(user_key)
    for i in range(20):
        iter_key = bytes(b ^ i for b in rc4_key)
        data = _rc4_crypt(iter_key, data)
    return data


def _compute_encryption_key_r3(
    user_key: str,
    owner_val: bytes,
    permissions: int,
    file_id: bytes,
) -> bytes:
    """ISO 32000-1 Algorithm 2: Computing an encryption key (128-bit, R=3)."""
    padded_user = _pad_pdf_key(user_key)
    m = hashlib.md5(usedforsecurity=False)
    m.update(padded_user)
    m.update(owner_val)
    m.update(struct.pack("<i", permissions))
    m.update(file_id)
    digest = m.digest()
    for _ in range(50):
        digest = hashlib.md5(digest[:16], usedforsecurity=False).digest()
    return digest[:16]


def _compute_user_value_r3(encryption_key: bytes, file_id: bytes) -> bytes:
    """ISO 32000-1 Algorithm 5: Computing the encryption dictionary's U (user key) value (R=3)."""
    digest = hashlib.md5(_PDF_PADDING + file_id, usedforsecurity=False).digest()
    data = _rc4_crypt(encryption_key, digest)
    for i in range(1, 20):
        iter_key = bytes(b ^ i for b in encryption_key)
        data = _rc4_crypt(iter_key, data)
    return data + _PDF_PADDING[:16]


def _encrypt_pdf_object_bytes(
    encryption_key: bytes,
    obj_num: int,
    gen_num: int,
    data: bytes,
) -> bytes:
    """ISO 32000-1 Algorithm 1: Encryption of data using the RC4 algorithm per indirect object."""
    obj_key_input = (
        encryption_key
        + struct.pack("<I", obj_num)[:3]
        + struct.pack("<H", gen_num)[:2]
    )
    obj_rc4_key = hashlib.md5(obj_key_input, usedforsecurity=False).digest()[:16]
    return _rc4_crypt(obj_rc4_key, data)

# ---------------------------------------------------------------------------
# HTML Report Structural Parser
# ---------------------------------------------------------------------------
EPHEMRES_VERSION = VERSION

# Template classes that mark something the reader must notice. In the PDF these render bold
# (danger also in red), so the meaning never depends on colour alone.
_FLAG_CLASSES = (
    ("pill-down", "danger"),
    ("color-util-danger", "danger"),
    ("pill-unstable", "warn"),
    ("color-util-warn", "warn"),
)
_BOLD_STYLE_RE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)")
_VOID_TAGS = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")
)


def _class_flag(classes: List[str]) -> str:
    for name, flag in _FLAG_CLASSES:
        if name in classes:
            return flag
    return ""


def _merge_flag(current: str, new: str) -> str:
    return "danger" if "danger" in (current, new) else (new or current)


@dataclass
class _Cell:
    text: str
    align: str = "left"  # left | right | center, from the template's text-right / text-center
    bold: bool = False
    flag: str = ""  # "" | "warn" | "danger"


@dataclass
class _Table:
    header: List[_Cell]
    rows: List[List[_Cell]]


@dataclass
class _ParsedReportDocument:
    title: str = "Ephemres Network Telemetry Report"
    footer_text: str = "Confidential — Internal Network Infrastructure Audit"
    company_name: str = "Enterprise Network Infrastructure"
    department: str = "Operations"
    report_type_label: str = ""
    period_label: str = ""
    generated_at_label: str = ""
    scope_title: str = ""
    scope_desc: str = ""
    # The shared endpoint filter the report was made with (v3.3.6), printed under the title.
    scope_filter: str = ""
    scope_badge: str = ""
    lang: str = "en"
    logo_bytes: Optional[bytes] = None
    logo_mime: str = ""
    logo_alt: str = ""
    kpis: List[Tuple[str, str, str]] = field(default_factory=list)
    # ("heading", (text, uppercase)), ("paragraph", text), ("note", text),
    # ("notes", [text, ...]), ("table", _Table)
    blocks: List[Tuple[str, Any]] = field(default_factory=list)


class _ReportHTMLParser(HTMLParser):
    """Reads the rendered report template into document parts, keeping the layout intent the
    template states in classes: column alignment, bold cells and status flags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = _ParsedReportDocument()
        self._stack: List[Tuple[str, List[str]]] = []
        self._text_buf: List[str] = []

        self._in_header = False
        self._in_scope = False
        self._in_kpi_card = False
        self._current_kpi: List[str] = ["", "", ""]
        self._notes: Optional[List[str]] = None

        self._in_table = False
        self._in_thead = False
        self._in_row = False
        self._cell: Optional[_Cell] = None
        self._table_header: List[_Cell] = []
        self._table_rows: List[List[_Cell]] = []
        self._row: List[_Cell] = []

    def _flush_text(self) -> str:
        raw = "".join(self._text_buf)
        self._text_buf.clear()
        return re.sub(r"\s+", " ", raw).strip()

    def _handle_img(self, attr_map: Dict[str, str]) -> None:
        # Validate any <img src="..."> via secure_url_fetcher (enforces SSRF/LFI defense)
        src = attr_map.get("src", "").strip()
        if not src:
            return
        try:
            res = secure_url_fetcher(src)
            self.doc.logo_bytes = res.raw_bytes
            self.doc.logo_mime = res.content_type
            self.doc.logo_alt = attr_map.get("alt", "").strip()
        except Exception:
            if getattr(secure_url_fetcher, "_fail_on_errors", True):
                raise

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = attr_map.get("class", "").split()

        if t == "html" and attr_map.get("lang"):
            self.doc.lang = attr_map["lang"].strip()
        if t == "img":
            self._handle_img(attr_map)
        if t in _VOID_TAGS:
            return
        self._stack.append((t, classes))

        if self._cell is not None:
            # Inline content of a table cell: <strong>, status pills, threshold colour classes.
            if t in ("strong", "b") or _BOLD_STYLE_RE.search(attr_map.get("style", "")):
                self._cell.bold = True
            self._cell.flag = _merge_flag(self._cell.flag, _class_flag(classes))
            return

        if t in ("style", "title"):
            self._text_buf.clear()
        elif "doc-header" in classes:
            self._in_header = True
        elif "scope-banner" in classes:
            self._in_scope = True
        elif "kpi-card" in classes:
            self._in_kpi_card = True
            self._current_kpi = ["", "", ""]
        elif "coverage-note" in classes:
            self._notes = []
            self._text_buf.clear()
        elif t == "table":
            self._in_table = True
            self._table_header = []
            self._table_rows = []
        elif t == "thead":
            self._in_thead = True
        elif t == "tbody":
            self._in_thead = False
        elif t == "tr" and self._in_table:
            self._in_row = True
            self._row = []
        elif t in ("th", "td") and self._in_row:
            align = "right" if "text-right" in classes else ("center" if "text-center" in classes else "left")
            bold = t == "th" or bool(_BOLD_STYLE_RE.search(attr_map.get("style", "")))
            self._cell = _Cell("", align=align, bold=bold, flag=_class_flag(classes))
            self._text_buf.clear()
        elif t in ("h1", "h2", "h3", "p", "div"):
            self._text_buf.clear()

    def handle_data(self, data: str) -> None:
        self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            return
        classes: List[str] = []
        # Pop to the matching start tag so stray or unclosed tags can't shift the stack.
        while self._stack:
            open_tag, open_classes = self._stack.pop()
            if open_tag == t:
                classes = open_classes
                break

        if t == "style":
            style_str = "".join(self._text_buf)
            self._text_buf.clear()
            m_footer = re.search(r'@bottom-left\s*\{[^}]*content:\s*"([^"]+)"', style_str)
            if m_footer:
                self.doc.footer_text = html.unescape(m_footer.group(1).strip())
            return

        if t == "title":
            txt = self._flush_text()
            if txt:
                self.doc.title = txt
            return

        if self._cell is not None:
            if t in ("th", "td"):
                self._cell.text = self._flush_text()
                self._row.append(self._cell)
                self._cell = None
            return

        if t == "tr" and self._in_row:
            self._in_row = False
            if self._row:
                if self._in_thead and not self._table_header:
                    self._table_header = list(self._row)
                else:
                    self._table_rows.append(list(self._row))
            self._row = []
            return

        if t == "table" and self._in_table:
            self._in_table = False
            self._in_thead = False
            self.doc.blocks.append(("table", _Table(self._table_header, self._table_rows)))
            return

        if self._in_kpi_card:
            if "kpi-label" in classes:
                self._current_kpi[0] = self._flush_text()
            elif "kpi-val" in classes:
                self._current_kpi[1] = self._flush_text()
            elif "kpi-sub" in classes:
                self._current_kpi[2] = self._flush_text()
            elif "kpi-card" in classes:
                self._in_kpi_card = False
                if any(self._current_kpi):
                    self.doc.kpis.append(
                        (self._current_kpi[0], self._current_kpi[1], self._current_kpi[2])
                    )
            return

        if self._in_scope:
            if "scope-title" in classes:
                self.doc.scope_title = self._flush_text()
            elif "scope-filter" in classes:
                self.doc.scope_filter = self._flush_text()
            elif "scope-desc" in classes:
                self.doc.scope_desc = self._flush_text()
            elif "scope-badge" in classes:
                self.doc.scope_badge = self._flush_text()
            elif "scope-banner" in classes:
                self._in_scope = False
            return

        if self._in_header:
            if t == "h1":
                self.doc.company_name = self._flush_text()
            elif "department" in classes:
                self.doc.department = self._flush_text()
            elif "report-type" in classes:
                self.doc.report_type_label = self._flush_text()
            elif "meta-line" in classes:
                ml = self._flush_text()
                if ml.lower().startswith("period:"):
                    self.doc.period_label = ml
                elif ml.lower().startswith("generated:"):
                    self.doc.generated_at_label = ml
            elif "doc-header" in classes:
                self._in_header = False
            return

        if self._notes is not None:
            if "coverage-note" in classes:
                tail = self._flush_text()
                if tail:
                    self._notes.append(tail)
                if self._notes:
                    self.doc.blocks.append(("notes", self._notes))
                self._notes = None
            elif t in ("div", "p"):
                txt = self._flush_text()
                if txt:
                    self._notes.append(txt)
            return

        if "section-title" in classes:
            sec_txt = self._flush_text()
            if sec_txt:
                self.doc.blocks.append(("heading", (sec_txt, True)))
            return

        if t in ("h1", "h2", "h3"):
            h_txt = self._flush_text()
            if h_txt:
                self.doc.blocks.append(("heading", (h_txt, False)))
            return

        if "empty-state" in classes:
            txt = self._flush_text()
            if txt:
                self.doc.blocks.append(("note", txt))
            return

        if t in ("p", "div") and not self._in_table:
            p_txt = self._flush_text()
            if p_txt:
                self.doc.blocks.append(("paragraph", p_txt))


# ---------------------------------------------------------------------------
# Native PDF Typesetter: measured text, wrapped table cells, tagged structure
# ---------------------------------------------------------------------------
_PAGE_W = 595.28
_PAGE_H = 841.89
_MARGIN_X = 34.0
_CONTENT_W = _PAGE_W - 2 * _MARGIN_X
_CONTENT_TOP = _PAGE_H - 36.0
_CONTENT_BOTTOM = 54.0
_FOOTER_RULE_Y = 42.0

# Type scale (from base_report.html). 7pt is the floor for any text.
_BODY_SIZE, _BODY_LH = 8.5, 12.0
_SECTION_SIZE, _SECTION_LH, _SECTION_TRACKING = 9.0, 12.0, 0.4
_CELL_SIZE, _CELL_LH, _CELL_PAD_X, _CELL_PAD_Y = 7.5, 9.5, 5.0, 3.5
_HEAD_SIZE, _HEAD_LH, _HEAD_PAD_Y, _HEAD_TRACKING = 7.0, 9.0, 4.0, 0.25
_SMALL_SIZE, _SMALL_LH = 7.0, 9.0
_LABEL_TRACKING = 0.3
_BLOCK_GAP = 10.0

# Ephemres slate palette (contrast on white in brackets)
_INK = "0.059 0.090 0.165"  # #0f172a  (17:1)
_BODY = "0.118 0.161 0.231"  # #1e293b  (14:1)
_CELL_INK = "0.200 0.255 0.333"  # #334155 (10:1)
_SUBTLE = "0.278 0.333 0.412"  # #475569 (7.6:1)
_MUTED = "0.392 0.455 0.545"  # #64748b (4.8:1)
_ACCENT = "0.145 0.388 0.922"  # #2563eb (5.2:1)
_DANGER = "0.725 0.110 0.110"  # #b91c1c (6.5:1), always paired with bold
_RULE = "0.796 0.835 0.882"  # #cbd5e1
_HAIR = "0.886 0.910 0.941"  # #e2e8f0
_FILL_HEAD = "0.945 0.961 0.976"  # #f1f5f9
_FILL_ALT = "0.973 0.980 0.988"  # #f8fafc

_FIT_SAMPLE_ROWS = 400
_REFINE_SAMPLE_ROWS = 150
_REFINE_PASSES = 6
_REFINE_TARGETS = 4
# Credit for un-wrapping a cell whose row stays as tall (a line saved in the row counts 1).
_UNWRAP_CREDIT = 0.05
_TH_ATTRS = "/A << /O /Table /Scope /Column >>"


def _pdf_literal(encoded: str) -> str:
    """Escape WinAnsi-encoded text for a PDF literal string."""
    return encoded.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", " ").replace("\n", " ")


def _utf16_hex(text: str) -> str:
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def _extract_pdf_image_info(raw_bytes: bytes, mime: str = "") -> Optional[Dict[str, Any]]:
    """
    Zero-dependency pure-Python raster image parser supporting JPEG and PNG.
    Extracts dimensions, colorspace, and bit depth, and formats raw streams for PDF /Image XObjects.
    """
    if not raw_bytes or len(raw_bytes) < 8:
        return None

    # 1. JPEG: \xff\xd8
    if raw_bytes[:2] == b"\xff\xd8":
        try:
            idx = 2
            length = len(raw_bytes)
            while idx < length - 1:
                if raw_bytes[idx] != 0xFF:
                    idx += 1
                    continue
                marker = raw_bytes[idx + 1]
                idx += 2
                if marker in (0xD8, 0xD9, 0x00):
                    continue
                if idx + 2 > length:
                    break
                seg_len = struct.unpack(">H", raw_bytes[idx : idx + 2])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    if idx + seg_len > length:
                        break
                    bits, height, width, components = struct.unpack(">BHHB", raw_bytes[idx + 2 : idx + 8])
                    colorspace = "/ColorSpace /DeviceRGB" if components == 3 else "/ColorSpace /DeviceGray"
                    return {
                        "width": width,
                        "height": height,
                        "filter": "/Filter /DCTDecode",
                        "colorspace": colorspace,
                        "bits": bits,
                        "data": raw_bytes,
                    }
                idx += seg_len
        except Exception as exc:
            logger.debug("Failed to decode JPEG image: %s", exc)
            return None

    # 2. PNG: \x89PNG\r\n\x1a\n
    if raw_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            if len(raw_bytes) < 33 or raw_bytes[12:16] != b"IHDR":
                return None
            width, height, bit_depth, color_type = struct.unpack(">IIBB", raw_bytes[16:26])

            idx = 33
            idat_chunks: List[bytes] = []
            while idx < len(raw_bytes) - 8:
                chunk_len = struct.unpack(">I", raw_bytes[idx : idx + 4])[0]
                chunk_type = raw_bytes[idx + 4 : idx + 8]
                chunk_data = raw_bytes[idx + 8 : idx + 8 + chunk_len]
                if chunk_type == b"IDAT":
                    idat_chunks.append(chunk_data)
                elif chunk_type == b"IEND":
                    break
                idx += 12 + chunk_len

            if not idat_chunks:
                return None

            decompressed = zlib.decompress(b"".join(idat_chunks))

            channel_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
            channels = channel_map.get(color_type, 3)
            bpp = max(1, (channels * bit_depth) // 8)
            stride = 1 + width * bpp

            raw_rgb = bytearray()
            prior_row = bytearray(width * bpp)

            for row_idx in range(height):
                row_start = row_idx * stride
                if row_start >= len(decompressed):
                    break
                filter_type = decompressed[row_start]
                row_raw = decompressed[row_start + 1 : row_start + stride]
                unfiltered = bytearray(width * bpp)

                for col in range(len(row_raw)):
                    raw_val = row_raw[col]
                    a = unfiltered[col - bpp] if col >= bpp else 0
                    b = prior_row[col]
                    c = prior_row[col - bpp] if col >= bpp else 0

                    if filter_type == 0:
                        val = raw_val
                    elif filter_type == 1:
                        val = (raw_val + a) & 0xFF
                    elif filter_type == 2:
                        val = (raw_val + b) & 0xFF
                    elif filter_type == 3:
                        val = (raw_val + ((a + b) // 2)) & 0xFF
                    elif filter_type == 4:
                        p = a + b - c
                        pa = abs(p - a)
                        pb = abs(p - b)
                        pc = abs(p - c)
                        pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                        val = (raw_val + pr) & 0xFF
                    else:
                        val = raw_val
                    unfiltered[col] = val

                prior_row = unfiltered

                if color_type == 6:  # RGBA
                    for px in range(0, len(unfiltered), 4):
                        r, g, b, a = unfiltered[px : px + 4]
                        alpha = a / 255.0
                        out_r = int(r * alpha + 255 * (1.0 - alpha))
                        out_g = int(g * alpha + 255 * (1.0 - alpha))
                        out_b = int(b * alpha + 255 * (1.0 - alpha))
                        raw_rgb.extend([out_r, out_g, out_b])
                elif color_type == 2:  # RGB
                    raw_rgb.extend(unfiltered)
                elif color_type == 0:  # Grayscale
                    for px in range(len(unfiltered)):
                        g = unfiltered[px]
                        raw_rgb.extend([g, g, g])
                elif color_type == 4:  # Gray + Alpha
                    for px in range(0, len(unfiltered), 2):
                        g, a = unfiltered[px : px + 2]
                        alpha = a / 255.0
                        out_g = int(g * alpha + 255 * (1.0 - alpha))
                        raw_rgb.extend([out_g, out_g, out_g])
                else:
                    raw_rgb.extend(unfiltered)

            compressed_rgb = zlib.compress(bytes(raw_rgb))
            return {
                "width": width,
                "height": height,
                "filter": "/Filter /FlateDecode",
                "colorspace": "/ColorSpace /DeviceRGB",
                "bits": 8,
                "data": compressed_rgb,
            }
        except Exception as exc:
            logger.debug("Failed to decode PNG image: %s", exc)
            return None

    return None


def _baseline(top: float, size: float, line_h: float) -> float:
    """Baseline that centres Helvetica's glyph box (ascender 718, descender -207) in a line box."""
    return top - (line_h - 0.925 * size) / 2.0 - 0.718 * size


@dataclass
class _Run:
    """Styled text, encoded to WinAnsi and wrapped to its box."""

    lines: List[str]
    font: str
    size: float
    line_h: float
    color: str
    tracking: float = 0.0
    source: str = ""
    display: str = ""

    @property
    def height(self) -> float:
        return len(self.lines) * self.line_h

    def width(self) -> float:
        return max((text_width(line, self.font, self.size, self.tracking) for line in self.lines), default=0.0)

    def actual_text(self) -> Optional[str]:
        """What assistive technology should read when the drawn text differs from the source:
        uppercased for display, wrapped onto several lines, or holding characters WinAnsi lacks."""
        if len(self.lines) > 1 or self.display != self.source or not is_lossless(self.source):
            return self.source
        return None


def _run(
    text: str,
    font: str,
    size: float,
    line_h: float,
    color: str,
    width: Optional[float] = None,
    tracking: float = 0.0,
    upper: bool = False,
) -> _Run:
    source = str(text or "")
    display = source.upper() if upper else source
    encoded, font = shape(display, font)
    lines = wrap_text(encoded, font, size, width, tracking) if width is not None else [encoded]
    return _Run(lines, font, size, line_h, color, tracking, source, display)


def _text_op(encoded: str, font: str, size: float, x: float, y: float, color: str, tracking: float = 0.0) -> str:
    ttf = embedded_font(font)
    if ttf is not None:
        # Embedded fonts use Identity-H: two-byte glyph IDs in a hex string, drawn at the
        # size that matches Helvetica's x-height.
        string = "<" + "".join(f"{ttf.glyph(c):04X}" for c in encoded) + ">"
        size = round(size * size_factor(font), 3)
    else:
        string = f"({_pdf_literal(encoded)})"
    return (
        f"BT {color} rg {font} {size:g} Tf {tracking:g} Tc 1 0 0 1 {x:.2f} {y:.2f} Tm "
        f"{string} Tj ET"
    )


def _run_ops(run: _Run, x: float, top: float, width: float = 0.0, align: str = "left") -> List[str]:
    ops = []
    for idx, line in enumerate(run.lines):
        if not line:
            continue
        lw = text_width(line, run.font, run.size, run.tracking)
        if align == "right":
            lx = x + width - lw
        elif align == "center":
            lx = x + (width - lw) / 2.0
        else:
            lx = x
        y = _baseline(top - idx * run.line_h, run.size, run.line_h)
        ops.append(_text_op(line, run.font, run.size, lx, y, run.color, run.tracking))
    return ops


def _rect(x: float, y: float, w: float, h: float, fill: Optional[str] = None, stroke: Optional[str] = None, lw: float = 0.75) -> str:
    if fill and stroke:
        return f"{fill} rg {stroke} RG {lw:g} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re B"
    if fill:
        return f"{fill} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f"
    return f"{stroke} RG {lw:g} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S"


def _hline(x0: float, x1: float, y: float, color: str, lw: float) -> str:
    return f"{color} RG {lw:g} w {x0:.2f} {y:.2f} m {x1:.2f} {y:.2f} l S"


@dataclass
class _CellMetrics:
    """Pre-measured pieces of a cell's text, so line counts can be simulated without
    re-measuring strings: word widths, each word's break segments, and the space width."""

    total: float
    words: List[float]
    segments: List[List[float]]
    space: float


def _measure_cell(encoded: str, font: str, size: float, tracking: float = 0.0, unbreakable: bool = False) -> _CellMetrics:
    total = text_width(encoded, font, size, tracking)
    if unbreakable:
        return _CellMetrics(total, [total], [[total]], 0.0)
    words = encoded.split(" ")
    return _CellMetrics(
        total,
        [text_width(w, font, size, tracking) for w in words],
        [[text_width(seg, font, size, tracking) for seg in segments(w)] for w in words],
        text_width(" ", font, size, tracking),
    )


def _count_lines(m: _CellMetrics, avail: float) -> int:
    """Lines wrap_text would produce at this width: greedy at spaces, then at separators.
    Exact while every separator segment fits the width; a segment wider than the column
    (only when the table cannot fit at all) is split by width, approximating wrap_text's
    character breaks."""
    if m.total <= avail + 0.01:
        return 1
    avail = max(avail, 1.0)
    lines, cur = 1, 0.0
    for word, segs in zip(m.words, m.segments):
        add = word if cur == 0.0 else m.space + word
        if cur + add <= avail + 0.01:
            cur += add
            continue
        if word <= avail + 0.01:
            lines += 1 if cur > 0.0 else 0
            cur = word
            continue
        if cur > 0.0:
            lines += 1
            cur = 0.0
        for seg in segs:
            if cur + seg <= avail + 0.01:
                cur += seg
                continue
            if cur > 0.0:
                lines += 1
            if seg <= avail + 0.01:
                cur = seg
            else:
                extra = math.ceil(seg / avail) - 1
                lines += extra
                cur = seg - extra * avail
    return lines


def _width_for_lines(m: _CellMetrics, k: int) -> float:
    """Narrowest text width at which the cell wraps to at most k lines."""
    lo, hi = m.total / k, m.total
    if _count_lines(m, lo) <= k:
        return lo
    for _ in range(14):
        mid = (lo + hi) / 2.0
        if _count_lines(m, mid) <= k:
            hi = mid
        else:
            lo = mid
    return hi


def _fit_columns(
    mins: List[float],
    maxs: List[float],
    total: float,
    cells: List[List[_CellMetrics]],
    weights: List[float],
    pad: float,
) -> List[float]:
    """Choose column widths that make the table as short as possible.

    Each column starts at its narrowest unbreakable word (mins) and may grow to its widest
    single line (maxs). Spare width goes to whichever column saves the most wrapped lines per
    point of width, looking ahead to the next width at which one of its cells loses a line
    (cells: one list per row, header included with a higher weight). Lines that make a row
    shorter count most; un-wrapping a cell in a row that stays as tall counts a little. Line
    counts simulate the real greedy wrap from pre-measured word widths, and each cell caches
    the width at which it next loses a line, so a step only touches cells that change.
    """
    n = len(mins)
    if sum(maxs) <= total:
        extra = total - sum(maxs)
        base = sum(maxs) or 1.0
        return [m + extra * m / base for m in maxs]
    if sum(mins) >= total:
        scale = total / (sum(mins) or 1.0)
        return [m * scale for m in mins]

    # Fit on an evenly spaced sample of a very long table (always keeping the header row).
    cells, weights = _sample_rows(cells, weights, _FIT_SAMPLE_ROWS)

    widths = list(mins)
    inf = float("inf")
    rows = range(len(cells))
    lines = [[_count_lines(row[c], widths[c] - pad) for c in range(n)] for row in cells]

    def next_width(ri: int, c: int) -> float:
        cur = lines[ri][c]
        return _width_for_lines(cells[ri][c], cur - 1) + pad if cur > 1 else inf

    need = [[next_width(ri, c) for c in range(n)] for ri in rows]

    def row_stats(row_lines: List[int]) -> Tuple[int, int, int]:
        top = max(row_lines)
        count = row_lines.count(top)
        second = max((v for v in row_lines if v != top), default=top if count > 1 else 1)
        return top, count, second

    stats = [row_stats(r) for r in lines]
    slack = total - sum(widths)
    min_step = max(0.5, slack / 200.0)
    while slack > 1e-6:
        best: Optional[int] = None
        best_ratio = 0.0
        best_inc = 0.0
        for c in range(n):
            room = min(slack, maxs[c] - widths[c])
            if room <= 1e-6:
                continue
            nxt = min((need[ri][c] for ri in rows), default=inf) - widths[c] + 1e-6
            inc = min(max(nxt, min_step), room)
            new_w = widths[c] + inc
            gain = 0.0
            for ri in rows:
                if need[ri][c] > new_w:
                    continue
                cur = lines[ri][c]
                new_l = _count_lines(cells[ri][c], new_w - pad)
                if new_l >= cur:
                    continue
                top, count, second = stats[ri]
                shorter_row = (top - max(new_l, second)) if (cur == top and count == 1) else 0
                gain += (shorter_row + _UNWRAP_CREDIT * (cur - new_l)) * weights[ri]
            if gain > 0.0 and gain / inc > best_ratio:
                best, best_ratio, best_inc = c, gain / inc, inc
        if best is None:
            break
        widths[best] += best_inc
        slack -= best_inc
        for ri in rows:
            if need[ri][best] <= widths[best]:
                lines[ri][best] = _count_lines(cells[ri][best], widths[best] - pad)
                need[ri][best] = next_width(ri, best)
                stats[ri] = row_stats(lines[ri])
    # Greedy growth can stop in a local optimum: also score two plain layouts and refine
    # from whichever makes the shortest table.
    if slack > 1e-6:
        base = sum(widths) or 1.0
        widths = [w + slack * w / base for w in widths]
        slack = 0.0

    candidates = [widths, _clamped_share(maxs, mins, total), _clamped_share([1.0] * n, mins, total)]
    widths = list(min(candidates, key=lambda ws: _table_cost(ws, cells, weights, pad)))
    for ri, row in enumerate(cells):
        lines[ri] = [_count_lines(row[c], widths[c] - pad) for c in range(n)]
        need[ri] = [next_width(ri, c) for c in range(n)]

    _refine_columns(widths, mins, maxs, cells, weights, pad, lines, need, next_width)
    if slack > 1e-6:
        base = sum(widths) or 1.0
        widths = [w + slack * w / base for w in widths]
    return widths


def _clamped_share(shares: List[float], mins: List[float], total: float) -> List[float]:
    """Split total in proportion to shares, but never below a column's minimum."""
    widths = [0.0] * len(shares)
    fixed = [False] * len(shares)
    while True:
        free_total = total - sum(m for m, f in zip(mins, fixed) if f)
        free_share = sum(sh for sh, f in zip(shares, fixed) if not f) or 1.0
        changed = False
        for i, (sh, mn) in enumerate(zip(shares, mins)):
            if fixed[i]:
                widths[i] = mn
                continue
            widths[i] = free_total * sh / free_share
            if widths[i] < mn:
                fixed[i] = True
                changed = True
        if not changed:
            return widths


def _refine_columns(widths, mins, maxs, cells, weights, pad, lines, need, next_width) -> None:
    """Fix what the greedy pass can't see: move width from one column to another when the
    move makes the table shorter overall (for example, take 15pt from a long router name
    column so "443 / HTTPS" stops wrapping on every row). Works in place on a sample of rows."""
    n = len(widths)
    rows = list(range(min(len(cells), _REFINE_SAMPLE_ROWS)))

    def row_cost(row_lines: List[int], weight: float) -> float:
        return weight * (max(row_lines) + _UNWRAP_CREDIT * sum(row_lines))

    for _ in range(_REFINE_PASSES):
        improved = False
        for b in range(n):
            # Try the next few widths at which some cell of column b loses a line.
            targets = sorted({need[ri][b] for ri in rows if need[ri][b] <= maxs[b] + 1e-6})[:_REFINE_TARGETS]
            best_a, best_change, best_lines, delta = None, -1e-9, None, 0.0
            for target in targets:
                d = target - widths[b] + 1e-6
                if d <= 0.0:
                    continue
                for a in range(n):
                    if a == b or widths[a] - d < mins[a] - 1e-6:
                        continue
                    change = 0.0
                    trial = []
                    for ri in rows:
                        la = _count_lines(cells[ri][a], widths[a] - d - pad)
                        lb = _count_lines(cells[ri][b], widths[b] + d - pad)
                        new_row = list(lines[ri])
                        new_row[a], new_row[b] = la, lb
                        change += row_cost(new_row, weights[ri]) - row_cost(lines[ri], weights[ri])
                        trial.append((la, lb))
                    if change < best_change:
                        best_a, best_change, best_lines, delta = a, change, trial, d
            if best_a is None:
                continue
            widths[best_a] -= delta
            widths[b] += delta
            for ri, (la, lb) in zip(rows, best_lines):
                lines[ri][best_a], lines[ri][b] = la, lb
            for ri in range(len(cells)):
                if ri >= len(rows):
                    lines[ri][best_a] = _count_lines(cells[ri][best_a], widths[best_a] - pad)
                    lines[ri][b] = _count_lines(cells[ri][b], widths[b] - pad)
                need[ri][best_a] = next_width(ri, best_a)
                need[ri][b] = next_width(ri, b)
            improved = True
        if not improved:
            break


def _sample_rows(cells: List[Any], weights: List[float], limit: int) -> Tuple[List[Any], List[float]]:
    """An evenly spaced sample of a long table's rows (always keeping the last, the header)."""
    if len(cells) <= limit:
        return cells, weights
    stride = len(cells) / float(limit - 1)
    picks = sorted({int(i * stride) for i in range(limit - 1)} | {len(cells) - 1})
    return [cells[i] for i in picks], [weights[i] for i in picks]


def _table_cost(widths: List[float], cells: List[List[Any]], weights: List[float], pad: float) -> float:
    """Weighted table height in lines, with a small credit for every cell kept on one line."""
    cost = 0.0
    for row, weight in zip(cells, weights):
        counts = [_count_lines(m, w - pad) for m, w in zip(row, widths)]
        cost += weight * (max(counts) + _UNWRAP_CREDIT * sum(counts))
    return cost


def _equalize_twin_columns(
    widths: List[float],
    mins: List[float],
    data_max: List[float],
    data_min: List[float],
    cells: List[List[Any]],
    weights: List[float],
    pad: float,
) -> List[float]:
    """Give columns holding the same kind of data (start and end timestamps, say) the same
    width, so one does not wrap while its twin stays on one line. Twins have matching widest
    cell and widest word. Both take the wider width (paid by the column with most to spare)
    or both the narrower (the difference going to the widest other column), whichever makes
    the shorter table."""
    cells, weights = _sample_rows(cells, weights, _FIT_SAMPLE_ROWS)
    n = len(widths)
    widths = list(widths)
    for a in range(n):
        for b in range(a + 1, n):
            if data_max[a] <= 0 or data_max[b] <= 0:
                continue
            twins = abs(data_max[a] - data_max[b]) <= 0.03 * max(data_max[a], data_max[b]) and \
                abs(data_min[a] - data_min[b]) <= 0.03 * max(data_min[a], data_min[b])
            diff = abs(widths[a] - widths[b])
            if not twins or diff <= 0.5:
                continue
            wide, narrow = (a, b) if widths[a] > widths[b] else (b, a)
            others = [c for c in range(n) if c not in (a, b)]
            options = []
            donors = [c for c in others if widths[c] - diff >= mins[c] - 1e-6]
            if donors:
                donor = max(donors, key=lambda c: widths[c] - mins[c])
                up = list(widths)
                up[narrow] += diff
                up[donor] -= diff
                options.append(up)
            if others and widths[narrow] >= mins[wide] - 1e-6:
                receiver = max(others, key=lambda c: widths[c])
                down = list(widths)
                down[wide] -= diff
                down[receiver] += diff
                options.append(down)
            if options:
                # Lowest cost wins; on a tie the first option (both wider) is kept.
                widths = min(options, key=lambda ws: _table_cost(ws, cells, weights, pad))
    return widths


class _StructElem:
    """A node of the tagged-PDF structure tree. Kids are elements or (page index, MCID)."""

    def __init__(self, role: str, parent: Optional["_StructElem"] = None, attrs: str = "", alt: Optional[str] = None) -> None:
        self.role = role
        self.parent = parent
        self.attrs = attrs
        self.alt = alt
        self.kids: List[Any] = []
        self.obj_id = 0
        if parent is not None:
            parent.kids.append(self)


@dataclass
class _Page:
    index: int
    ops: List[str] = field(default_factory=list)
    mcids: List[_StructElem] = field(default_factory=list)


@dataclass
class _PreparedTable:
    widths: List[float]
    header: List[_Cell]
    head_runs: Optional[List[_Run]]
    head_h: float
    body: List[Tuple[List[_Cell], List[_Run], float]]

    @property
    def keep(self) -> float:
        """Height that must fit before the table may start: header plus two rows."""
        return self.head_h + sum(h for _, _, h in self.body[:2])


class _Typesetter:
    """Lays a parsed report out on A4 pages. Every piece of text is measured, so text wraps
    instead of being cut, columns are sized to their content, and numbers align right.
    Content is tagged (headings, paragraphs, tables, figure); decoration and page furniture
    are marked as artifacts so assistive technology skips them."""

    def __init__(self, doc: _ParsedReportDocument, logo_info: Optional[Dict[str, Any]] = None) -> None:
        self.doc = doc
        self.logo = logo_info
        self.pages: List[_Page] = []
        self.root = _StructElem("Document")
        self.page: _Page = _Page(0)
        self.y = 0.0
        self.page_top = 0.0

    # -- page and marked-content primitives ---------------------------------
    def _new_page(self) -> None:
        self.page = _Page(len(self.pages))
        self.pages.append(self.page)
        if self.page.index == 0:
            self._first_page_header()
        else:
            self._running_header()
        self.page_top = self.y

    def _fits(self, height: float) -> bool:
        return self.y - height >= _CONTENT_BOTTOM

    def _artifact(self, ops: List[str], pagination: bool = False) -> None:
        if not ops:
            return
        self.page.ops.append("/Artifact << /Type /Pagination >> BDC" if pagination else "/Artifact BMC")
        self.page.ops.extend(ops)
        self.page.ops.append("EMC")

    def _tag(self, elem: _StructElem, ops: List[str], actual_text: Optional[str] = None) -> None:
        if not ops:
            return
        mcid = len(self.page.mcids)
        self.page.mcids.append(elem)
        elem.kids.append((self.page.index, mcid))
        props = f"/MCID {mcid}"
        if actual_text is not None:
            props += f" /ActualText {_utf16_hex(actual_text)}"
        self.page.ops.append(f"/{elem.role} << {props} >> BDC")
        self.page.ops.extend(ops)
        self.page.ops.append("EMC")

    def _tag_run(self, elem: _StructElem, run: _Run, x: float, top: float, width: float = 0.0, align: str = "left") -> None:
        self._tag(elem, _run_ops(run, x, top, width, align), run.actual_text())

    # -- page furniture -----------------------------------------------------
    def _first_page_header(self) -> None:
        d = self.doc
        top = _CONTENT_TOP
        brand = _run(f"ephemres v{EPHEMRES_VERSION}", FONT_BOLD, 12.0, 15.0, _INK)
        metas = [_run(m, FONT_REGULAR, 7.5, 10.0, _MUTED) for m in (d.period_label, d.generated_at_label) if m]
        right_w = max([brand.width()] + [m.width() for m in metas])

        logo_w = logo_h = 0.0
        if self.logo:
            orig_w = max(1, self.logo.get("width", 100))
            orig_h = max(1, self.logo.get("height", 100))
            scale = min(120.0 / orig_w, 38.0 / orig_h, 1.0)
            logo_w, logo_h = orig_w * scale, orig_h * scale
        text_x = _MARGIN_X + (logo_w + 10.0 if logo_w else 0.0)
        left_w = _CONTENT_W - (text_x - _MARGIN_X) - right_w - 18.0

        company = _run(d.company_name, FONT_BOLD, 13.0, 16.0, _INK, left_w)
        dept = _run(d.department, FONT_REGULAR, 8.0, 10.5, _MUTED, left_w) if d.department else None
        rtype = (
            _run(d.report_type_label, FONT_BOLD, 8.0, 10.5, _ACCENT, left_w, _LABEL_TRACKING, upper=True)
            if d.report_type_label
            else None
        )
        left_h = company.height + (dept.height if dept else 0.0) + (rtype.height + 1.0 if rtype else 0.0)
        right_h = brand.height + sum(m.height for m in metas)
        block_h = max(left_h, right_h, logo_h)

        if self.logo:
            alt = d.logo_alt or f"{d.company_name} logo"
            figure = _StructElem("Figure", self.root, attrs=(
                f"/A << /O /Layout /BBox [{_MARGIN_X:.2f} {top - (block_h + logo_h) / 2:.2f} "
                f"{_MARGIN_X + logo_w:.2f} {top - (block_h - logo_h) / 2:.2f}] >>"
            ), alt=alt)
            self._tag(figure, [
                f"q {logo_w:.2f} 0 0 {logo_h:.2f} {_MARGIN_X:.2f} {top - (block_h + logo_h) / 2:.2f} cm /ImLogo Do Q"
            ])

        y = top
        self._tag_run(_StructElem("P", self.root), company, text_x, y)
        y -= company.height
        if dept:
            self._tag_run(_StructElem("P", self.root), dept, text_x, y)
            y -= dept.height
        if rtype:
            self._tag_run(_StructElem("H1", self.root), rtype, text_x, y - 1.0)

        right_x = _PAGE_W - _MARGIN_X
        self._artifact(_run_ops(brand, right_x - right_w, top, right_w, "right"))
        y = top - brand.height
        for meta in metas:
            self._tag_run(_StructElem("P", self.root), meta, right_x - right_w, y, right_w, "right")
            y -= meta.height

        rule_y = top - block_h - 6.0
        self._artifact([_hline(_MARGIN_X, right_x, rule_y, _RULE, 1.2)])
        self.y = rule_y - 12.0
        self._scope_banner()
        self._kpi_strip()

    def _running_header(self) -> None:
        d = self.doc
        top = _CONTENT_TOP
        right = _run(d.period_label, FONT_REGULAR, _SMALL_SIZE, _SMALL_LH, _MUTED)
        label = " · ".join(p for p in (d.company_name, d.report_type_label) if p)
        left = _run(label, FONT_REGULAR, _SMALL_SIZE, _SMALL_LH, _MUTED, _CONTENT_W - right.width() - 16.0)
        ops = _run_ops(left, _MARGIN_X, top) + _run_ops(right, _MARGIN_X, top, _CONTENT_W, "right")
        rule_y = top - max(left.height, right.height) - 5.0
        ops.append(_hline(_MARGIN_X, _PAGE_W - _MARGIN_X, rule_y, _HAIR, 0.75))
        self._artifact(ops, pagination=True)
        self.y = rule_y - 12.0

    def _footers(self) -> None:
        total = len(self.pages)
        for page in self.pages:
            self.page = page
            label = _run(
                f"Page {page.index + 1} of {total} · Generated by Ephemres v{EPHEMRES_VERSION}",
                FONT_REGULAR, _SMALL_SIZE, _SMALL_LH, _MUTED,
            )
            foot = _run(self.doc.footer_text, FONT_REGULAR, _SMALL_SIZE, _SMALL_LH, _MUTED, _CONTENT_W - label.width() - 16.0)
            foot.lines = foot.lines[:2]
            top = _FOOTER_RULE_Y - 4.0
            ops = [_hline(_MARGIN_X, _PAGE_W - _MARGIN_X, _FOOTER_RULE_Y, _HAIR, 0.5)]
            ops += _run_ops(foot, _MARGIN_X, top)
            ops += _run_ops(label, _MARGIN_X, top, _CONTENT_W, "right")
            self._artifact(ops, pagination=True)

    # -- first-page summary -------------------------------------------------
    def _scope_banner(self) -> None:
        d = self.doc
        if not (d.scope_title or d.scope_desc):
            return
        pad_x, pad_y = 8.0, 6.0
        inner = _CONTENT_W - 2 * pad_x
        badge = _run(d.scope_badge, FONT_BOLD, 7.5, 11.0, _SUBTLE, None, _LABEL_TRACKING) if d.scope_badge else None
        title_w = inner - (badge.width() + 12.0 if badge else 0.0)
        title = _run(d.scope_title, FONT_BOLD, 8.5, 11.0, _INK, title_w, _LABEL_TRACKING, upper=True)
        # The filter line is in ink, not muted: it says which endpoints the numbers cover.
        filt = _run(d.scope_filter, FONT_REGULAR, 7.5, 10.0, _INK, inner) if d.scope_filter else None
        desc = _run(d.scope_desc, FONT_REGULAR, 7.5, 10.0, _MUTED, inner) if d.scope_desc else None
        h = 2 * pad_y + title.height + (2.0 + filt.height if filt else 0.0) + (2.0 + desc.height if desc else 0.0)
        top = self.y
        self._artifact([_rect(_MARGIN_X, top - h, _CONTENT_W, h, _FILL_ALT, _HAIR)])
        self._tag_run(_StructElem("P", self.root), title, _MARGIN_X + pad_x, top - pad_y)
        if badge:
            self._tag_run(_StructElem("P", self.root), badge, _MARGIN_X + pad_x, top - pad_y, inner, "right")
        line_top = top - pad_y - title.height - 2.0
        if filt:
            self._tag_run(_StructElem("P", self.root), filt, _MARGIN_X + pad_x, line_top)
            line_top -= filt.height + 2.0
        if desc:
            self._tag_run(_StructElem("P", self.root), desc, _MARGIN_X + pad_x, line_top)
        self.y = top - h - 12.0

    def _kpi_strip(self) -> None:
        kpis = self.doc.kpis
        if not kpis:
            return
        gap, pad_x, pad_y = 8.0, 8.0, 6.0
        card_w = (_CONTENT_W - gap * (len(kpis) - 1)) / len(kpis)
        inner = card_w - 2 * pad_x
        cards = []
        for label, value, sub in kpis:
            label_run = _run(label, FONT_BOLD, _SMALL_SIZE, _SMALL_LH, _MUTED, inner, _LABEL_TRACKING, upper=True)
            size = 13.0
            value_enc, value_font = shape(value, FONT_BOLD)
            while size > 9.0 and text_width(value_enc, value_font, size) > inner:
                size -= 0.5
            value_run = _run(value, FONT_BOLD, size, size + 3.0, _INK, inner)
            sub_run = _run(sub, FONT_REGULAR, _SMALL_SIZE, _SMALL_LH, _MUTED, inner) if sub else None
            cards.append((label_run, value_run, sub_run))
        content_h = max(
            lr.height + 2.0 + vr.height + (sr.height if sr else 0.0) for lr, vr, sr in cards
        )
        card_h = content_h + 2 * pad_y
        top = self.y
        for idx, (label_run, value_run, sub_run) in enumerate(cards):
            x = _MARGIN_X + idx * (card_w + gap)
            self._artifact([_rect(x, top - card_h, card_w, card_h, "1 1 1", _HAIR)])
            elem = _StructElem("P", self.root)
            y = top - pad_y
            self._tag_run(elem, label_run, x + pad_x, y)
            y -= label_run.height + 2.0
            self._tag_run(elem, value_run, x + pad_x, y)
            y -= value_run.height
            if sub_run:
                self._tag_run(elem, sub_run, x + pad_x, y)
        self.y = top - card_h - 14.0

    # -- body blocks --------------------------------------------------------
    def _prepare_table(self, tbl: _Table) -> Optional[_PreparedTable]:
        ncols = max([len(tbl.header)] + [len(r) for r in tbl.rows])
        if ncols == 0:
            return None
        pad = 2 * _CELL_PAD_X

        def padded(cells: List[_Cell]) -> List[_Cell]:
            return list(cells) + [_Cell("") for _ in range(ncols - len(cells))]

        header = padded(tbl.header)
        rows = [padded(r) for r in tbl.rows]
        has_header = any(c.text for c in header)

        def cell_style(c: _Cell) -> Tuple[str, str]:
            font = FONT_BOLD if (c.bold or c.flag) else FONT_REGULAR
            return font, (_DANGER if c.flag == "danger" else _CELL_INK)

        data_max = [0.0] * ncols
        min_words = [0.0] * ncols
        min_segments = [0.0] * ncols
        metrics: List[List[_CellMetrics]] = []
        weights: List[float] = []
        for row in rows:
            row_metrics = []
            for i, cell in enumerate(row):
                enc, font = shape(cell.text, cell_style(cell)[0])
                # Numbers and states (right/centre aligned) keep their unit on the same line.
                m = _measure_cell(enc, font, _CELL_SIZE, unbreakable=cell.align != "left")
                row_metrics.append(m)
                data_max[i] = max(data_max[i], m.total + pad)
                min_words[i] = max(min_words[i], max(m.words) + pad)
                min_segments[i] = max(min_segments[i], max(max(seg) for seg in m.segments) + pad)
            metrics.append(row_metrics)
            weights.append(1.0)

        head_full = [0.0] * ncols
        head_word = [0.0] * ncols
        head_segment = [0.0] * ncols
        if has_header:
            head_metrics = []
            for i, cell in enumerate(header):
                head_enc, head_font = shape(cell.text.upper(), FONT_BOLD)
                m = _measure_cell(head_enc, head_font, _HEAD_SIZE, _HEAD_TRACKING)
                head_metrics.append(m)
                head_full[i] = m.total + pad
                head_word[i] = max(m.words) + pad
                head_segment[i] = max(max(seg) for seg in m.segments) + pad
            # A wrapped header costs one line once, but it is the line every column is read by.
            metrics.append(head_metrics)
            weights.append(3.0)

        maxs = [max(dm, hf) for dm, hf in zip(data_max, head_full)]
        # Prefer breaking only at spaces; break identifiers at . - : / only if the table needs it.
        mins = [min(max(mw, hw), mx) for mw, hw, mx in zip(min_words, head_word, maxs)]
        if sum(mins) > _CONTENT_W:
            mins = [min(max(ms, hs), mx) for ms, hs, mx in zip(min_segments, head_segment, maxs)]
        widths = _fit_columns(mins, maxs, _CONTENT_W, metrics, weights, pad)
        widths = _equalize_twin_columns(widths, mins, data_max, min_words, metrics, weights, pad)

        head_runs = None
        head_h = 0.0
        if has_header:
            head_runs = [
                _run(c.text, FONT_BOLD, _HEAD_SIZE, _HEAD_LH, _SUBTLE, widths[i] - pad, _HEAD_TRACKING, upper=True)
                for i, c in enumerate(header)
            ]
            head_h = 2 * _HEAD_PAD_Y + max(r.height for r in head_runs)

        body = []
        for row in rows:
            runs = []
            for i, cell in enumerate(row):
                font, color = cell_style(cell)
                runs.append(_run(cell.text, font, _CELL_SIZE, _CELL_LH, color, widths[i] - pad))
            body.append((row, runs, 2 * _CELL_PAD_Y + max(r.height for r in runs)))
        return _PreparedTable(widths, header, head_runs, head_h, body)

    def _table_header(self, t: _PreparedTable, thead: Optional[_StructElem]) -> None:
        """Draw the header row: tagged the first time, a pagination artifact when repeated."""
        top = self.y
        x_end = _PAGE_W - _MARGIN_X
        self._artifact([
            _rect(_MARGIN_X, top - t.head_h, _CONTENT_W, t.head_h, _FILL_HEAD),
            _hline(_MARGIN_X, x_end, top - t.head_h, _RULE, 0.75),
        ])
        tr = _StructElem("TR", thead) if thead is not None else None
        repeated: List[str] = []
        x = _MARGIN_X
        for cell, run, w in zip(t.header, t.head_runs or [], t.widths):
            ops = _run_ops(run, x + _CELL_PAD_X, top - _HEAD_PAD_Y, w - 2 * _CELL_PAD_X, cell.align)
            if tr is not None:
                th = _StructElem("TH", tr, attrs=_TH_ATTRS)
                self._tag(th, ops, run.actual_text())
            else:
                repeated.extend(ops)
            x += w
        self._artifact(repeated, pagination=True)
        self.y -= t.head_h

    def _draw_table(self, t: _PreparedTable) -> None:
        if not self._fits(t.keep) and self.y < self.page_top:
            self._new_page()
        table = _StructElem("Table", self.root)
        thead = _StructElem("THead", table) if t.head_runs else None
        tbody = _StructElem("TBody", table)
        x_end = _PAGE_W - _MARGIN_X
        seg_top = self.y
        if t.head_runs:
            self._table_header(t, thead)
        rows_on_page = 0
        last = len(t.body) - 1
        for idx, (row, runs, h) in enumerate(t.body):
            # Never leave the last row alone at the top of a page: if the last two rows do not
            # fit together, carry both over (as long as two rows stay behind with the header).
            widow = idx == last - 1 and rows_on_page >= 2 and not self._fits(h + t.body[last][2])
            if not self._fits(h) or widow:
                self._artifact([_rect(_MARGIN_X, self.y, _CONTENT_W, seg_top - self.y, stroke=_RULE)])
                self._new_page()
                seg_top = self.y
                if t.head_runs:
                    self._table_header(t, None)
                rows_on_page = 0
            rows_on_page += 1
            row_top = self.y
            decoration = []
            if idx % 2 == 1:
                decoration.append(_rect(_MARGIN_X, row_top - h, _CONTENT_W, h, _FILL_ALT))
            decoration.append(_hline(_MARGIN_X, x_end, row_top - h, _HAIR, 0.5))
            self._artifact(decoration)
            tr = _StructElem("TR", tbody)
            x = _MARGIN_X
            for cell, run, w in zip(row, runs, t.widths):
                td = _StructElem("TD", tr)
                self._tag_run(td, run, x + _CELL_PAD_X, row_top - _CELL_PAD_Y, w - 2 * _CELL_PAD_X, cell.align)
                x += w
            self.y -= h
        self._artifact([_rect(_MARGIN_X, self.y, _CONTENT_W, seg_top - self.y, stroke=_RULE)])
        self.y -= _BLOCK_GAP

    def _draw_heading(self, text: str, upper: bool, keep_next: float) -> None:
        run = _run(text, FONT_BOLD, _SECTION_SIZE, _SECTION_LH, _BODY, _CONTENT_W, _SECTION_TRACKING, upper=upper)
        gap = 4.0 if self.y < self.page_top else 0.0
        # Keep the heading with the start of what follows it.
        if not self._fits(gap + run.height + 5.0 + keep_next) and self.y < self.page_top:
            self._new_page()
            gap = 0.0
        self.y -= gap
        self._tag_run(_StructElem("H2", self.root), run, _MARGIN_X, self.y)
        self.y -= run.height + 5.0

    def _draw_paragraph(self, run: _Run) -> None:
        elem = _StructElem("P", self.root)
        if self._fits(run.height):
            self._tag_run(elem, run, _MARGIN_X, self.y)
            self.y -= run.height + 4.0
            return
        lines = list(run.lines)
        while lines:
            room = int((self.y - _CONTENT_BOTTOM) // run.line_h)
            if room < min(2, len(lines)):
                self._new_page()
                continue
            chunk = _Run(lines[:room], run.font, run.size, run.line_h, run.color, run.tracking, "", "")
            self._tag(elem, _run_ops(chunk, _MARGIN_X, self.y))
            self.y -= chunk.height
            lines = lines[room:]
        self.y -= 4.0

    def _draw_box(self, runs: List[_Run], align: str, pad: float = 8.0) -> None:
        inner = _CONTENT_W - 2 * pad
        h = 2 * pad + sum(r.height for r in runs) + 3.0 * (len(runs) - 1)
        if not self._fits(h) and self.y < self.page_top:
            self._new_page()
        top = self.y
        self._artifact([_rect(_MARGIN_X, top - h, _CONTENT_W, h, _FILL_ALT, _HAIR)])
        y = top - pad
        for run in runs:
            self._tag_run(_StructElem("P", self.root), run, _MARGIN_X + pad, y, inner, align)
            y -= run.height + 3.0
        self.y = top - h - _BLOCK_GAP

    def run(self) -> Tuple[List[_Page], _StructElem]:
        self._new_page()
        box_w = _CONTENT_W - 16.0
        prepared: List[Tuple[str, Any, float]] = []
        for kind, payload in self.doc.blocks:
            if kind == "table":
                t = self._prepare_table(payload)
                if t is not None:
                    prepared.append(("table", t, t.keep))
            elif kind == "heading":
                prepared.append(("heading", payload, 0.0))
            elif kind == "paragraph":
                r = _run(payload, FONT_REGULAR, _BODY_SIZE, _BODY_LH, _BODY, _CONTENT_W)
                prepared.append(("paragraph", r, min(2, len(r.lines)) * r.line_h))
            elif kind == "note":
                r = _run(payload, FONT_REGULAR, 8.0, 11.0, _MUTED, box_w)
                prepared.append(("box", ([r], "center"), r.height + 16.0))
            elif kind == "notes":
                runs = [_run(n, FONT_REGULAR, 7.5, 10.0, _CELL_INK, box_w) for n in payload]
                prepared.append(("box", (runs, "left"), sum(r.height for r in runs) + 16.0))

        for idx, (kind, data, _) in enumerate(prepared):
            keep_next = prepared[idx + 1][2] if idx + 1 < len(prepared) else 0.0
            if kind == "table":
                self._draw_table(data)
            elif kind == "heading":
                self._draw_heading(data[0], data[1], keep_next)
            elif kind == "paragraph":
                self._draw_paragraph(data)
            elif kind == "box":
                self._draw_box(data[0], data[1])

        self._footers()
        return self.pages, self.root


_EMBEDDED_TJ = re.compile(r"(/F[34]) [\d.]+ Tf [-\d.]+ Tc 1 0 0 1 \S+ \S+ Tm <([0-9A-F]*)> Tj")


def _to_unicode_cmap(ttf: Any, gids: List[int]) -> bytes:
    """A /ToUnicode CMap so text in an embedded font copies, searches and reads as Unicode."""
    entries = [f"<{g:04X}> <{ttf.char_for(g).encode('utf-16-be').hex().upper()}>" for g in gids if g]
    blocks = []
    for i in range(0, len(entries), 100):
        chunk = entries[i: i + 100]
        blocks.append(f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar")
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + "\n".join(blocks)
        + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")


def _compile_pdf(
    pages: List[_Page],
    root: _StructElem,
    title: str,
    lang: str = "en",
    logo_info: Optional[Dict[str, Any]] = None,
    encrypted: bool = True,
    user_password: str = "",
    owner_password: Optional[str] = None,
    permissions: int = PDF_READONLY_PERMISSIONS,
) -> bytes:
    """
    Assembles a tagged PDF 1.5 document: page streams, the structure tree (/StructTreeRoot,
    /ParentTree, /MarkInfo), /Lang, /DisplayDocTitle, /Info metadata, and optionally the
    Standard Security Handler (V=2, R=3, 128-bit RC4) with read-only permissions that still
    let assistive technology extract text.
    """
    file_id = hashlib.md5(f"{title}-{secrets.token_hex(8)}".encode("utf-8"), usedforsecurity=False).digest()
    enc_key: Optional[bytes] = None
    if encrypted:
        owner_pwd = owner_password or secrets.token_hex(32)
        owner_val = _compute_owner_value_r3(user_password, owner_pwd)
        enc_key = _compute_encryption_key_r3(user_password, owner_val, permissions, file_id)
        user_val = _compute_user_value_r3(enc_key, file_id)

    # 1 Catalog, 2 Pages, 3 /F1 Helvetica, 4 /F2 Helvetica-Bold, 5 /Info, 6 /Encrypt (if encrypted)
    next_id = 7 if enc_key is not None else 6

    def alloc() -> int:
        nonlocal next_id
        next_id += 1
        return next_id - 1

    def pdf_str(obj_id: int, text: str) -> str:
        """A text string: PDFDocEncoding when ASCII, else UTF-16BE; encrypted when the file is."""
        raw = text.encode("ascii") if text.isascii() else b"\xfe\xff" + text.encode("utf-16-be")
        if enc_key is not None:
            raw = _encrypt_pdf_object_bytes(enc_key, obj_id, 0, raw)
        return f"<{raw.hex()}>"

    logo_obj_id = alloc() if logo_info else None

    # Embedded fallback fonts: the glyphs each one draws across all pages.
    used_glyphs: Dict[str, Set[int]] = {}
    for page in pages:
        for op in page.ops:
            m = _EMBEDDED_TJ.search(op)
            if m:
                hexed = m.group(2)
                used_glyphs.setdefault(m.group(1), set()).update(
                    int(hexed[i: i + 4], 16) for i in range(0, len(hexed), 4)
                )
    font_ids: Dict[str, Dict[str, int]] = {
        res: {part: alloc() for part in ("type0", "cid", "descriptor", "program", "tounicode")}
        for res in sorted(used_glyphs)
        if embedded_font(res) is not None
    }
    font_resources = "/F1 3 0 R /F2 4 0 R" + "".join(f" {res} {ids['type0']} 0 R" for res, ids in font_ids.items())

    page_ids: List[int] = []
    content_ids: List[int] = []
    for _ in pages:
        page_ids.append(alloc())
        content_ids.append(alloc())
    struct_root_id = alloc()
    parent_tree_id = alloc()
    elems: List[_StructElem] = []

    def number(elem: _StructElem) -> None:
        elem.obj_id = alloc()
        elems.append(elem)
        for kid in elem.kids:
            if isinstance(kid, _StructElem):
                number(kid)

    number(root)

    objects: List[Tuple[int, bytes]] = []
    res_xobject = f" /XObject << /ImLogo {logo_obj_id} 0 R >>" if logo_obj_id else ""
    for page, page_id, content_id in zip(pages, page_ids, content_ids):
        objects.append((page_id, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
            f"/Resources << /Font << {font_resources} >>{res_xobject} >> "
            f"/Contents {content_id} 0 R /StructParents {page.index} /Tabs /S >>"
        ).encode("ascii")))
        stream_bytes = "\n".join(page.ops).encode("latin-1", errors="replace")
        if enc_key is not None:
            encrypted_stream = _encrypt_pdf_object_bytes(enc_key, content_id, 0, zlib.compress(stream_bytes))
            content_obj = (
                f"<< /Length {len(encrypted_stream)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
                + encrypted_stream
                + b"\nendstream"
            )
        else:
            content_obj = (
                f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream"
            )
        objects.append((content_id, content_obj))

    if logo_obj_id and logo_info:
        img_data = logo_info["data"]
        if enc_key is not None:
            img_data = _encrypt_pdf_object_bytes(enc_key, logo_obj_id, 0, img_data)
        objects.append((logo_obj_id, (
            f"<< /Type /XObject /Subtype /Image /Width {logo_info['width']} /Height {logo_info['height']} "
            f"{logo_info['colorspace']} /BitsPerComponent {logo_info['bits']} "
            f"{logo_info['filter']} /Length {len(img_data)} >>\nstream\n"
        ).encode("ascii") + img_data + b"\nendstream"))

    def stream(obj_id: int, data: bytes, extra: str = "") -> bytes:
        packed = zlib.compress(data)
        if enc_key is not None:
            packed = _encrypt_pdf_object_bytes(enc_key, obj_id, 0, packed)
        return (
            f"<< /Length {len(packed)} /Filter /FlateDecode{extra} >>\nstream\n".encode("ascii")
            + packed
            + b"\nendstream"
        )

    for res, ids in font_ids.items():
        ttf = embedded_font(res)
        gids = sorted(used_glyphs[res] | {0})
        tag_seed = hashlib.md5(f"{res}:{gids}".encode("ascii"), usedforsecurity=False).digest()
        base = "".join(chr(65 + b % 26) for b in tag_seed[:6]) + "+" + ttf.postscript_name
        program = ttf.subset(gids)
        widths = " ".join(f"{g} [{ttf.advance_1000(g):.0f}]" for g in gids)
        bbox = " ".join(str(ttf.scale(v)) for v in ttf.bbox)
        objects.append((ids["type0"], (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
            f"/DescendantFonts [{ids['cid']} 0 R] /ToUnicode {ids['tounicode']} 0 R >>"
        ).encode("ascii")))
        objects.append((ids["cid"], (
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} "
            f"/CIDSystemInfo << /Registry {pdf_str(ids['cid'], 'Adobe')} /Ordering {pdf_str(ids['cid'], 'Identity')} "
            f"/Supplement 0 >> /FontDescriptor {ids['descriptor']} 0 R /DW 1000 /W [{widths}] "
            f"/CIDToGIDMap /Identity >>"
        ).encode("ascii")))
        objects.append((ids["descriptor"], (
            f"<< /Type /FontDescriptor /FontName /{base} /Flags 32 /FontBBox [{bbox}] "
            f"/ItalicAngle {ttf.italic_angle:g} /Ascent {ttf.scale(ttf.ascent)} /Descent {ttf.scale(ttf.descent)} "
            f"/CapHeight {ttf.scale(ttf.cap_height)} /StemV {120 if res == FONT_FALLBACK_BOLD else 80} "
            f"/FontFile2 {ids['program']} 0 R >>"
        ).encode("ascii")))
        objects.append((ids["program"], stream(ids["program"], program, f" /Length1 {len(program)}")))
        objects.append((ids["tounicode"], stream(ids["tounicode"], _to_unicode_cmap(ttf, gids))))

    # Structure tree
    objects.append((struct_root_id, (
        f"<< /Type /StructTreeRoot /K [{root.obj_id} 0 R] /ParentTree {parent_tree_id} 0 R "
        f"/ParentTreeNextKey {len(pages)} >>"
    ).encode("ascii")))
    nums = " ".join(
        f"{page.index} [{' '.join(f'{e.obj_id} 0 R' for e in page.mcids)}]" for page in pages
    )
    objects.append((parent_tree_id, f"<< /Nums [{nums}] >>".encode("ascii")))
    for elem in elems:
        parent_id = elem.parent.obj_id if elem.parent is not None else struct_root_id
        kids: List[str] = []
        first_page: Optional[int] = None
        for kid in elem.kids:
            if isinstance(kid, _StructElem):
                kids.append(f"{kid.obj_id} 0 R")
            else:
                page_idx, mcid = kid
                first_page = page_idx if first_page is None else first_page
                kids.append(f"<< /Type /MCR /Pg {page_ids[page_idx]} 0 R /MCID {mcid} >>")
        body = f"<< /Type /StructElem /S /{elem.role} /P {parent_id} 0 R /K [{' '.join(kids)}]"
        if first_page is not None:
            body += f" /Pg {page_ids[first_page]} 0 R"
        if elem.attrs:
            body += f" {elem.attrs}"
        if elem.alt:
            body += f" /Alt {pdf_str(elem.obj_id, elem.alt)}"
        objects.append((elem.obj_id, (body + " >>").encode("ascii")))

    kids_refs = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append((1, (
        f"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot {struct_root_id} 0 R "
        f"/MarkInfo << /Marked true >> /Lang {pdf_str(1, lang or 'en')} "
        f"/ViewerPreferences << /DisplayDocTitle true >> >>"
    ).encode("ascii")))
    objects.append((2, f"<< /Type /Pages /Kids [{kids_refs}] /Count {len(page_ids)} >>".encode("ascii")))
    objects.append((3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))
    objects.append((4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"))

    now_str = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    info_fields = {
        "/Title": title,
        "/Author": "Ephemres Enterprise Platform",
        "/Producer": f"Ephemres Reporting Engine v{EPHEMRES_VERSION}",
        "/CreationDate": now_str,
        "/ModDate": now_str,
    }
    objects.append((5, f"<< {' '.join(f'{k} {pdf_str(5, v)}' for k, v in info_fields.items())} >>".encode("ascii")))
    if enc_key is not None:
        objects.append((6, (
            f"<< /Filter /Standard /V 2 /R 3 /Length 128 /P {permissions} "
            f"/O <{owner_val.hex()}> /U <{user_val.hex()}> >>"
        ).encode("ascii")))

    objects.sort(key=lambda item: item[0])

    buf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for obj_id, obj_body in objects:
        offsets[obj_id] = len(buf)
        buf.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        buf.extend(obj_body)
        buf.extend(b"\nendobj\n")

    xref_offset = len(buf)
    total_objs = len(objects) + 1
    buf.extend(f"xref\n0 {total_objs}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for obj_id, _ in objects:
        buf.extend(f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii"))

    id_hex = file_id.hex()
    encrypt_ref = "/Encrypt 6 0 R " if enc_key is not None else ""
    buf.extend((
        f"trailer\n<< /Size {total_objs} /Root 1 0 R /Info 5 0 R {encrypt_ref}"
        f"/ID [<{id_hex}> <{id_hex}>] >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii"))
    return bytes(buf)


# ---------------------------------------------------------------------------
# Pure-Python PDF Inspector / Reader (Drop-in for pypdf.PdfReader in tests)
# ---------------------------------------------------------------------------
class _NativePdfPage:
    def __init__(self, text_content: str) -> None:
        self._text = text_content

    def extract_text(self) -> str:
        return self._text


class NativePdfReader:
    """
    Lightweight, zero-dependency PDF reader and Standard Security Handler
    verifier supporting .is_encrypted, .decrypt(password), .pages, .metadata, and .permissions.
    """

    def __init__(self, stream_or_bytes: Any) -> None:
        if isinstance(stream_or_bytes, (bytes, bytearray)):
            self._raw = bytes(stream_or_bytes)
        elif hasattr(stream_or_bytes, "read"):
            self._raw = stream_or_bytes.read()
        else:
            with open(str(stream_or_bytes), "rb") as f:
                self._raw = f.read()

        if not self._raw.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF header.")

        self._objects: Dict[int, bytes] = {}
        for m in re.finditer(rb"(\d+)\s+0\s+obj\r?\n(.*?)\r?\nendobj", self._raw, re.DOTALL):
            self._objects[int(m.group(1))] = m.group(2)

        m_encrypt = re.search(rb"/Encrypt\s+(\d+)\s+0\s+R", self._raw)
        self.is_encrypted = m_encrypt is not None
        self._encrypt_obj_id = int(m_encrypt.group(1)) if m_encrypt else None

        m_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", self._raw)
        self._info_obj_id = int(m_info.group(1)) if m_info else None

        m_id = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", self._raw)
        self._file_id = bytes.fromhex(m_id.group(1).decode("ascii")) if m_id else b""

        self._enc_key: Optional[bytes] = None
        self.permissions: Optional[int] = None
        self._owner_val = b""
        self._user_val = b""

        if self.is_encrypted and self._encrypt_obj_id in self._objects:
            enc_body = self._objects[self._encrypt_obj_id]
            m_p = re.search(rb"/P\s+(-?\d+)", enc_body)
            if m_p:
                self.permissions = int(m_p.group(1))
            m_o = re.search(rb"/O\s*<([0-9a-fA-F]+)>", enc_body)
            if m_o:
                self._owner_val = bytes.fromhex(m_o.group(1).decode("ascii"))
            m_u = re.search(rb"/U\s*<([0-9a-fA-F]+)>", enc_body)
            if m_u:
                self._user_val = bytes.fromhex(m_u.group(1).decode("ascii"))

        self._pages_cache: Optional[List[_NativePdfPage]] = None
        self._metadata_cache: Optional[Dict[str, str]] = None

    def decrypt(self, password: str) -> int:
        if not self.is_encrypted:
            return 1
        candidate_key = _compute_encryption_key_r3(
            password,
            self._owner_val,
            self.permissions or PDF_READONLY_PERMISSIONS,
            self._file_id,
        )
        expected_u = _compute_user_value_r3(candidate_key, self._file_id)
        if expected_u[:16] == self._user_val[:16]:
            self._enc_key = candidate_key
            self._pages_cache = None
            self._metadata_cache = None
            return 1
        return 0

    def _ensure_key(self) -> Optional[bytes]:
        if self.is_encrypted and self._enc_key is None:
            self.decrypt("")
        return self._enc_key

    @property
    def metadata(self) -> Dict[str, str]:
        if self._metadata_cache is not None:
            return self._metadata_cache
        key = self._ensure_key()
        meta: Dict[str, str] = {}
        if self._info_obj_id and self._info_obj_id in self._objects:
            info_raw = self._objects[self._info_obj_id]
            for m in re.finditer(rb"(/[A-Za-z0-9_]+)\s*<([0-9a-fA-F]+)>", info_raw):
                k = m.group(1).decode("ascii")
                raw_bytes = bytes.fromhex(m.group(2).decode("ascii"))
                if key is not None:
                    raw_bytes = _encrypt_pdf_object_bytes(key, self._info_obj_id, 0, raw_bytes)
                if raw_bytes.startswith(b"\xfe\xff"):
                    meta[k] = raw_bytes[2:].decode("utf-16-be", errors="replace")
                else:
                    meta[k] = raw_bytes.decode("latin-1", errors="replace")
        self._metadata_cache = meta
        return meta

    def _stream(self, obj_id: int) -> bytes:
        body = self._objects.get(obj_id, b"")
        m_start = re.search(rb">>\s*stream\r?\n", body)
        if not m_start:
            return b""
        # Take exactly /Length bytes, as real readers do. Encrypted and compressed data is
        # arbitrary binary, so 1 stream in 256 ends in a 0x0D byte; matching up to
        # "\r?\nendstream" took that byte for the end-of-line marker and cut it off, and
        # zlib then failed on the truncated data. Indirect lengths ("/Length 9 0 R") are never
        # written here, so only those fall back to the delimiter match.
        stream_dict = body[: m_start.start()]
        m_len = re.search(rb"/Length\s+(\d+)(?!\d)(?!\s+\d+\s+R)", stream_dict)
        if m_len:
            data = body[m_start.end(): m_start.end() + int(m_len.group(1))]
        else:
            m_stream = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.DOTALL)
            if not m_stream:
                return b""
            data = m_stream.group(1)
        if self._enc_key is not None:
            data = _encrypt_pdf_object_bytes(self._enc_key, obj_id, 0, data)
        # Look for the filter in the dictionary only: the binary data could hold those bytes.
        if b"/FlateDecode" in stream_dict:
            data = zlib.decompress(data)
        return data

    def _font_maps(self, page_body: bytes) -> Dict[str, Dict[int, str]]:
        """Font resource name -> glyph code -> text, from each font's /ToUnicode CMap."""
        maps: Dict[str, Dict[int, str]] = {}
        m_fonts = re.search(rb"/Font\s*<<(.*?)>>", page_body, re.DOTALL)
        if not m_fonts:
            return maps
        for name, font_id in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", m_fonts.group(1)):
            m_tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", self._objects.get(int(font_id), b""))
            if not m_tu:
                continue
            cmap_text = self._stream(int(m_tu.group(1))).decode("latin-1", errors="replace")
            maps[name.decode("ascii")] = {
                int(code, 16): bytes.fromhex(text).decode("utf-16-be", errors="replace")
                for code, text in re.findall(r"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]+)>", cmap_text)
            }
        return maps

    @property
    def pages(self) -> List[_NativePdfPage]:
        if self._pages_cache is not None:
            return self._pages_cache
        self._ensure_key()
        extracted_pages: List[_NativePdfPage] = []
        for obj_id, obj_body in sorted(self._objects.items()):
            if b"/Type /Page" in obj_body and b"/Contents" in obj_body:
                m_c = re.search(rb"/Contents\s+(\d+)\s+0\s+R", obj_body)
                if not m_c:
                    continue
                c_id = int(m_c.group(1))
                if b"stream" not in self._objects.get(c_id, b""):
                    continue
                # The same /Length-based read as the fonts' /ToUnicode streams (see _stream).
                content_str = self._stream(c_id).decode("latin-1", errors="replace")
                to_unicode = self._font_maps(obj_body)
                text_fragments: List[str] = []
                current_font = ""
                for m_op in re.finditer(
                    r"/(F\d+)\s+[\d.]+\s+Tf|\(((?:\\.|[^\\)])*)\)\s*Tj|<([0-9A-Fa-f]*)>\s*Tj", content_str
                ):
                    if m_op.group(1):
                        current_font = m_op.group(1)
                    elif m_op.group(3) is not None:
                        # Embedded font: two-byte glyph IDs, decoded through the font's /ToUnicode.
                        cmap = to_unicode.get(current_font, {})
                        hexed = m_op.group(3)
                        text_fragments.append(
                            "".join(cmap.get(int(hexed[i: i + 4], 16), "\ufffd") for i in range(0, len(hexed), 4))
                        )
                    else:
                        frag = (
                            m_op.group(2)
                            .replace("\\(", "(")
                            .replace("\\)", ")")
                            .replace("\\\\", "\\")
                        )
                        # Standard fonts draw WinAnsiEncoding (cp1252).
                        frag = frag.encode("latin-1", errors="replace").decode("cp1252", errors="replace")
                        text_fragments.append(frag)
                extracted_pages.append(_NativePdfPage("\n".join(text_fragments)))
        self._pages_cache = extracted_pages
        return extracted_pages


def _render_pdf_sync(html_content: str, title: str, encrypted: bool = True) -> bytes:
    """
    Synchronous Zero-Dependency Native PDF pipeline:
    1. Parses rendered HTML report and validates any embedded asset URIs via secure_url_fetcher.
    2. Typesets header, KPI cards, headings and paginated tables with measured, wrapped text,
       tagging the content for assistive technology.
    3. Enforces document immutability via ISO 32000-1 Standard Security Handler (128-bit RC4, read-only
       permissions that still allow accessibility extraction) and stamps standard PDF metadata.
    """
    parser = _ReportHTMLParser()
    parser.feed(html_content)
    doc = parser.doc
    if title:
        doc.title = title

    logo_info: Optional[Dict[str, Any]] = None
    if doc.logo_bytes:
        logo_info = _extract_pdf_image_info(doc.logo_bytes, doc.logo_mime)

    pages, struct_root = _Typesetter(doc, logo_info=logo_info).run()
    return _compile_pdf(
        pages=pages,
        root=struct_root,
        title=doc.title,
        lang=doc.lang,
        logo_info=logo_info,
        encrypted=encrypted,
        user_password="",
        owner_password=secrets.token_hex(32),
        permissions=PDF_READONLY_PERMISSIONS,
    )


async def render_pdf_async(html_content: str, title: str = "Ephemres Network Telemetry Report") -> bytes:
    """
    Asynchronously executes PDF rendering offloaded to a worker thread.
    Guarantees that CPU-bound rasterization never starves the FastAPI event loop.
    Enforces a strict 60-second timeout.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_render_pdf_sync, html_content, title, True),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        logger.error("PDF generation timed out after 60 seconds")
        raise TimeoutError("PDF report rendering exceeded maximum execution threshold (60s).")


class HTML:
    """Drop-in Weasyprint-compatible interface for native PDF generation."""
    def __init__(
        self,
        string: Optional[str] = None,
        filename: Optional[str] = None,
        url_fetcher: Any = None,
        **kwargs: Any,
    ) -> None:
        if string is not None:
            self.html_content = string
        elif filename is not None:
            with open(filename, "r", encoding="utf-8") as f:
                self.html_content = f.read()
        else:
            self.html_content = ""
        self.url_fetcher = url_fetcher

    def write_pdf(self, target: Any = None, stylesheets: Any = None, encrypted: bool = False, **kwargs: Any) -> bytes:
        pdf_bytes = _render_pdf_sync(self.html_content, title="Ephemres Network Telemetry Report", encrypted=encrypted)
        if target is not None:
            if hasattr(target, "write"):
                target.write(pdf_bytes)
            else:
                with open(target, "wb") as f:
                    f.write(pdf_bytes)
        return pdf_bytes

