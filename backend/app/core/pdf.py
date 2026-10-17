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
import mimetypes
import os
from pathlib import Path
import re
import secrets
import struct
import textwrap
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_to_bytes, urlparse
import zlib

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

# Read-only permissions: PRINT (bit 3 = 4) | PRINT_TO_REPRESENTATION (bit 12 = 2048) = 2052
# With reserved bits set per ISO 32000-1 Table 22 (0xFFFFF0C0 | 2052 = 0xFFFFF8C4 -> -1852 signed 32-bit)
PDF_READONLY_PERMISSIONS = -1852


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
    scope_badge: str = ""
    logo_bytes: Optional[bytes] = None
    logo_mime: str = ""
    kpis: List[Tuple[str, str, str]] = field(default_factory=list)
    blocks: List[Tuple[str, Any]] = field(default_factory=list)
    # blocks contains ("heading", text), ("paragraph", text), ("table", (headers, rows))


class _ReportHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.doc = _ParsedReportDocument()
        self._tag_stack: List[str] = []
        self._class_stack: List[str] = []
        self._text_buf: List[str] = []

        self._in_style = False
        self._in_title = False
        self._in_header = False
        self._in_scope = False
        self._in_kpi_card = False
        self._current_kpi: List[str] = ["", "", ""]

        self._in_table = False
        self._in_thead = False
        self._in_row = False
        self._in_cell = False
        self._cur_table_headers: List[str] = []
        self._cur_table_rows: List[List[str]] = []
        self._cur_row: List[str] = []

    def _flush_text(self) -> str:
        raw = "".join(self._text_buf)
        self._text_buf.clear()
        return re.sub(r"\s+", " ", raw).strip()

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls = attr_map.get("class", "")

        # Validate any <img src="..."> via secure_url_fetcher (enforces SSRF/LFI defense)
        if t == "img":
            src = attr_map.get("src", "").strip()
            if src:
                try:
                    res = secure_url_fetcher(src)
                    self.doc.logo_bytes = res.raw_bytes
                    self.doc.logo_mime = res.content_type
                except Exception:
                    if getattr(secure_url_fetcher, "_fail_on_errors", True):
                        raise

        self._tag_stack.append(t)
        self._class_stack.append(cls)

        if t == "style":
            self._in_style = True
            self._text_buf.clear()
        elif t == "title":
            self._in_title = True
            self._text_buf.clear()
        elif "doc-header" in cls:
            self._in_header = True
        elif "scope-banner" in cls:
            self._in_scope = True
        elif "kpi-card" in cls:
            self._in_kpi_card = True
            self._current_kpi = ["", "", ""]
        elif t == "table":
            self._in_table = True
            self._cur_table_headers = []
            self._cur_table_rows = []
        elif t == "thead":
            self._in_thead = True
        elif t == "tbody":
            self._in_thead = False
        elif t == "tr" and self._in_table:
            self._in_row = True
            self._cur_row = []
        elif t in ("th", "td") and self._in_row:
            self._in_cell = True
            self._text_buf.clear()
        elif t in ("h1", "h2", "h3", "p") or any(
            c in cls
            for c in (
                "department",
                "report-type",
                "meta-line",
                "scope-title",
                "scope-desc",
                "scope-badge",
                "kpi-label",
                "kpi-val",
                "kpi-sub",
                "section-title",
            )
        ):
            self._text_buf.clear()

    def handle_data(self, data: str) -> None:
        self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        cls = self._class_stack.pop() if self._class_stack else ""
        if self._tag_stack:
            self._tag_stack.pop()

        if t == "style":
            self._in_style = False
            style_str = "".join(self._text_buf)
            self._text_buf.clear()
            m_footer = re.search(r'@bottom-left\s*\{[^}]*content:\s*"([^"]+)"', style_str)
            if m_footer:
                self.doc.footer_text = html.unescape(m_footer.group(1).strip())
            return

        if t == "title":
            self._in_title = False
            txt = self._flush_text()
            if txt:
                self.doc.title = txt
            return

        if t in ("th", "td") and self._in_cell:
            self._in_cell = False
            cell_txt = self._flush_text()
            self._cur_row.append(cell_txt)
            return

        if t == "tr" and self._in_row:
            self._in_row = False
            if self._cur_row:
                if self._in_thead and not self._cur_table_headers:
                    self._cur_table_headers = list(self._cur_row)
                else:
                    self._cur_table_rows.append(list(self._cur_row))
            self._cur_row = []
            return

        if t == "table" and self._in_table:
            self._in_table = False
            self.doc.blocks.append(("table", (self._cur_table_headers, self._cur_table_rows)))
            return

        if self._in_kpi_card:
            if "kpi-label" in cls:
                self._current_kpi[0] = self._flush_text()
            elif "kpi-val" in cls:
                self._current_kpi[1] = self._flush_text()
            elif "kpi-sub" in cls:
                self._current_kpi[2] = self._flush_text()
            elif "kpi-card" in cls:
                self._in_kpi_card = False
                if any(self._current_kpi):
                    self.doc.kpis.append(
                        (self._current_kpi[0], self._current_kpi[1], self._current_kpi[2])
                    )
            return

        if self._in_scope:
            if "scope-title" in cls:
                self.doc.scope_title = self._flush_text()
            elif "scope-desc" in cls:
                self.doc.scope_desc = self._flush_text()
            elif "scope-badge" in cls:
                self.doc.scope_badge = self._flush_text()
            elif "scope-banner" in cls:
                self._in_scope = False
            return

        if self._in_header:
            if t == "h1":
                self.doc.company_name = self._flush_text()
            elif "department" in cls:
                self.doc.department = self._flush_text()
            elif "report-type" in cls:
                self.doc.report_type_label = self._flush_text()
            elif "meta-line" in cls:
                ml = self._flush_text()
                if ml.lower().startswith("period:"):
                    self.doc.period_label = ml
                elif ml.lower().startswith("generated:"):
                    self.doc.generated_at_label = ml
            elif "doc-header" in cls:
                self._in_header = False
            return

        if "section-title" in cls and "section-title-row" not in cls:
            sec_txt = self._flush_text()
            if sec_txt:
                self.doc.blocks.append(("heading", sec_txt))
            return

        if t in ("h1", "h2", "h3"):
            h_txt = self._flush_text()
            if h_txt:
                self.doc.blocks.append(("heading", h_txt))
            return

        if t in ("p", "div") and not self._in_table:
            p_txt = self._flush_text()
            if p_txt:
                self.doc.blocks.append(("paragraph", p_txt))


# ---------------------------------------------------------------------------
# Native PDF 1.4 Vector/Table Typesetter
# ---------------------------------------------------------------------------
def _pdf_escape_text(text: str) -> str:
    """Normalizes Unicode punctuation to WinAnsi/Latin-1 equivalents and escapes PDF literal parentheses."""
    replacements = {
        "—": "-",
        "–": "-",
        "·": ".",
        "•": "*",
        "“": '"',
        "”": '"',
        "’": "'",
        "‘": "'",
        "➔": "->",
    }
    s = str(text)
    for k, v in replacements.items():
        s = s.replace(k, v)
    s = s.encode("latin-1", errors="replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


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


def _typeset_document_pages(
    doc: _ParsedReportDocument,
    logo_info: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Typesets a parsed report document into one or more A4 Portrait (595.28 x 841.89 pt)
    PDF content streams using native vector drawing and Type1 fonts (/F1, /F2, /F3).
    """
    page_w = 595.28
    page_h = 841.89
    margin_x = 34.0
    content_w = page_w - 2 * margin_x
    bottom_margin = 52.0

    pages_ops: List[List[str]] = []
    cur_ops: List[str] = []
    y = page_h - 38.0

    def _start_new_page(is_first: bool = False) -> None:
        nonlocal cur_ops, y
        if cur_ops:
            pages_ops.append(cur_ops)
        cur_ops = []
        y = page_h - 38.0

        draw_w = 0.0
        draw_h = 0.0
        header_text_x = margin_x
        if logo_info:
            orig_w = max(1, logo_info.get("width", 100))
            orig_h = max(1, logo_info.get("height", 100))
            max_w = 95.0
            max_h = 30.0
            scale = min(max_w / orig_w, max_h / orig_h, 1.0)
            draw_w = orig_w * scale
            draw_h = orig_h * scale
            header_text_x = margin_x + draw_w + 10.0
            cur_ops.append(
                f"q {draw_w:.2f} 0 0 {draw_h:.2f} {margin_x:.2f} {y - draw_h + 10:.2f} cm /ImLogo Do Q"
            )

        # Corporate Header Banner
        cur_ops.append("0.06 0.09 0.16 rg")
        cur_ops.append(f"BT /F2 13 Tf {header_text_x:.2f} {y:.2f} Td ({_pdf_escape_text(doc.company_name)}) Tj ET")
        cur_ops.append(f"BT /F2 11 Tf {page_w - margin_x - 110:.2f} {y:.2f} Td (ephemres v3.3.2v) Tj ET")
        y -= 13.0

        if doc.department or doc.period_label:
            cur_ops.append("0.39 0.45 0.55 rg")
            cur_ops.append(f"BT /F1 8 Tf {header_text_x:.2f} {y:.2f} Td ({_pdf_escape_text(doc.department)}) Tj ET")
            if doc.period_label:
                cur_ops.append(
                    f"BT /F3 7.5 Tf {page_w - margin_x - 180:.2f} {y:.2f} Td ({_pdf_escape_text(doc.period_label)}) Tj ET"
                )
            y -= 11.0

        if doc.report_type_label or doc.generated_at_label:
            cur_ops.append("0.15 0.39 0.92 rg")
            cur_ops.append(
                f"BT /F2 8.5 Tf {header_text_x:.2f} {y:.2f} Td ({_pdf_escape_text(doc.report_type_label.upper())}) Tj ET"
            )
            if doc.generated_at_label:
                cur_ops.append("0.39 0.45 0.55 rg")
                cur_ops.append(
                    f"BT /F3 7.5 Tf {page_w - margin_x - 180:.2f} {y:.2f} Td ({_pdf_escape_text(doc.generated_at_label)}) Tj ET"
                )
            y -= 8.0

        # Header divider rule
        cur_ops.append("0.80 0.84 0.88 RG 1.2 w")
        cur_ops.append(f"{margin_x:.2f} {y:.2f} m {page_w - margin_x:.2f} {y:.2f} l S")
        y -= 14.0

        if is_first:
            # Scope Banner
            if doc.scope_title or doc.scope_desc:
                box_h = 30.0
                cur_ops.append("0.97 0.98 0.99 rg 0.89 0.91 0.94 RG 0.75 w")
                cur_ops.append(f"{margin_x:.2f} {y - box_h + 8:.2f} {content_w:.2f} {box_h:.2f} re B")
                cur_ops.append("0.06 0.09 0.16 rg")
                cur_ops.append(
                    f"BT /F2 8.5 Tf {margin_x + 8:.2f} {y - 4:.2f} Td ({_pdf_escape_text(doc.scope_title)}) Tj ET"
                )
                if doc.scope_badge:
                    cur_ops.append("0.28 0.33 0.41 rg")
                    cur_ops.append(
                        f"BT /F3 7.5 Tf {page_w - margin_x - 140:.2f} {y - 4:.2f} Td ({_pdf_escape_text(doc.scope_badge)}) Tj ET"
                    )
                if doc.scope_desc:
                    cur_ops.append("0.39 0.45 0.55 rg")
                    cur_ops.append(
                        f"BT /F1 7.2 Tf {margin_x + 8:.2f} {y - 16:.2f} Td ({_pdf_escape_text(doc.scope_desc[:105])}) Tj ET"
                    )
                y -= box_h + 8.0

            # KPI Summary Cards
            if doc.kpis:
                num_kpis = len(doc.kpis)
                gap = 8.0
                card_w = (content_w - gap * (num_kpis - 1)) / num_kpis
                card_h = 40.0
                for idx, (k_label, k_val, k_sub) in enumerate(doc.kpis):
                    cx = margin_x + idx * (card_w + gap)
                    cur_ops.append("1.0 1.0 1.0 rg 0.89 0.91 0.94 RG 0.75 w")
                    cur_ops.append(f"{cx:.2f} {y - card_h + 8:.2f} {card_w:.2f} {card_h:.2f} re B")
                    cur_ops.append("0.39 0.45 0.55 rg")
                    cur_ops.append(
                        f"BT /F2 6.8 Tf {cx + 6:.2f} {y - 3:.2f} Td ({_pdf_escape_text(k_label.upper())}) Tj ET"
                    )
                    cur_ops.append("0.06 0.09 0.16 rg")
                    cur_ops.append(
                        f"BT /F3 11.5 Tf {cx + 6:.2f} {y - 17:.2f} Td ({_pdf_escape_text(k_val)}) Tj ET"
                    )
                    cur_ops.append("0.58 0.64 0.72 rg")
                    cur_ops.append(
                        f"BT /F1 6.5 Tf {cx + 6:.2f} {y - 27:.2f} Td ({_pdf_escape_text(k_sub)}) Tj ET"
                    )
                y -= card_h + 10.0

    _start_new_page(is_first=True)

    for blk_type, payload in doc.blocks:
        if blk_type == "heading":
            if y < bottom_margin + 36.0:
                _start_new_page(is_first=False)
            y -= 4.0
            cur_ops.append("0.12 0.16 0.23 rg")
            cur_ops.append(f"BT /F2 9.5 Tf {margin_x:.2f} {y:.2f} Td ({_pdf_escape_text(payload)}) Tj ET")
            y -= 12.0

        elif blk_type == "paragraph":
            wrapped_lines = textwrap.wrap(payload, width=105) or [payload]
            for pline in wrapped_lines:
                if y < bottom_margin + 16.0:
                    _start_new_page(is_first=False)
                cur_ops.append("0.20 0.25 0.33 rg")
                cur_ops.append(f"BT /F1 8.5 Tf {margin_x:.2f} {y:.2f} Td ({_pdf_escape_text(pline)}) Tj ET")
                y -= 12.0
            y -= 2.0

        elif blk_type == "table":
            headers, rows = payload
            num_cols = max(len(headers), max((len(r) for r in rows), default=1), 1)

            col_max_lens = [len(str(h)) for h in headers]
            while len(col_max_lens) < num_cols:
                col_max_lens.append(5)
            for row in rows:
                for idx, cell in enumerate(row):
                    if idx < num_cols:
                        col_max_lens[idx] = max(col_max_lens[idx], len(str(cell)))
                    else:
                        col_max_lens.append(len(str(cell)))

            total_lens = sum(max(l, 3) for l in col_max_lens[:num_cols])
            col_widths = [(max(l, 3) / total_lens) * content_w for l in col_max_lens[:num_cols]]

            is_wide_table = num_cols >= 7
            font_size = 6.2 if is_wide_table else 7.2
            header_font_size = 6.0 if is_wide_table else 6.8
            row_h = 13.0 if is_wide_table else 15.0
            col_max_chars = [max(20, int(w / 3.1)) for w in col_widths]

            def _draw_table_header() -> None:
                nonlocal y
                if not headers:
                    return
                cur_ops.append("0.95 0.96 0.98 rg 0.80 0.84 0.88 RG 0.6 w")
                cur_ops.append(f"{margin_x:.2f} {y - row_h + 4:.2f} {content_w:.2f} {row_h:.2f} re B")
                cur_ops.append("0.28 0.33 0.41 rg")
                tx = margin_x
                for c_idx, h_text in enumerate(headers[:num_cols]):
                    cw = col_widths[c_idx]
                    mc = col_max_chars[c_idx]
                    cur_ops.append(
                        f"BT /F2 {header_font_size:.1f} Tf {tx + 3.0:.2f} {y - (row_h / 2.0) - 1.0:.2f} Td ({_pdf_escape_text(h_text[:mc].upper())}) Tj ET"
                    )
                    tx += cw
                y -= row_h

            if y < bottom_margin + row_h * 2:
                _start_new_page(is_first=False)

            _draw_table_header()

            for r_idx, row_cells in enumerate(rows):
                if y < bottom_margin + row_h:
                    _start_new_page(is_first=False)
                    _draw_table_header()

                if r_idx % 2 == 1:
                    cur_ops.append("0.98 0.98 0.99 rg 0.89 0.91 0.94 RG 0.4 w")
                else:
                    cur_ops.append("1.0 1.0 1.0 rg 0.89 0.91 0.94 RG 0.4 w")
                cur_ops.append(f"{margin_x:.2f} {y - row_h + 4:.2f} {content_w:.2f} {row_h:.2f} re B")

                cur_ops.append("0.20 0.25 0.33 rg")
                tx = margin_x
                for c_idx, cell_val in enumerate(row_cells[:num_cols]):
                    cw = col_widths[c_idx]
                    mc = col_max_chars[c_idx]
                    font_ref = "/F2" if c_idx == 0 else "/F1"
                    cur_ops.append(
                        f"BT {font_ref} {font_size:.1f} Tf {tx + 3.0:.2f} {y - (row_h / 2.0) - 1.0:.2f} Td ({_pdf_escape_text(str(cell_val)[:mc])}) Tj ET"
                    )
                    tx += cw
                y -= row_h

            y -= 8.0

    if cur_ops:
        pages_ops.append(cur_ops)

    # Stamp footer with Page X of Y on every page
    total_pages = len(pages_ops)
    final_streams: List[str] = []
    for idx, ops in enumerate(pages_ops, start=1):
        footer_y = 26.0
        ops.append("0.85 0.88 0.92 RG 0.5 w")
        ops.append(f"{margin_x:.2f} {footer_y + 10:.2f} m {page_w - margin_x:.2f} {footer_y + 10:.2f} l S")
        ops.append("0.39 0.45 0.55 rg")
        ops.append(
            f"BT /F1 7.2 Tf {margin_x:.2f} {footer_y:.2f} Td ({_pdf_escape_text(doc.footer_text)}) Tj ET"
        )
        page_label = f"Page {idx} of {total_pages} . Generated by Ephemres v3.3.2v"
        ops.append(
            f"BT /F3 7.2 Tf {page_w - margin_x - 185:.2f} {footer_y:.2f} Td ({_pdf_escape_text(page_label)}) Tj ET"
        )
        final_streams.append("\n".join(ops))

    return final_streams


def _compile_encrypted_pdf_1_4(
    page_streams: List[str],
    title: str,
    logo_info: Optional[Dict[str, Any]] = None,
    encrypted: bool = True,
    user_password: str = "",
    owner_password: Optional[str] = None,
    permissions: int = PDF_READONLY_PERMISSIONS,
) -> bytes:
    """
    Assembles a complete ISO 32000-1 PDF 1.4 document with page streams, /Info metadata,
    and optional Standard Security Handler (V=2, R=3, 128-bit RC4).
    """
    file_id = hashlib.md5(f"{title}-{secrets.token_hex(8)}".encode("utf-8"), usedforsecurity=False).digest()
    if encrypted:
        owner_pwd = owner_password or secrets.token_hex(32)
        owner_val = _compute_owner_value_r3(user_password, owner_pwd)
        enc_key = _compute_encryption_key_r3(user_password, owner_val, permissions, file_id)
        user_val = _compute_user_value_r3(enc_key, file_id)
    else:
        enc_key = None

    objects: List[Tuple[int, bytes]] = []

    # Object IDs:
    # 1: Catalog
    # 2: Pages
    # 3: Font /F1 (Helvetica)
    # 4: Font /F2 (Helvetica-Bold)
    # 5: Font /F3 (Courier)
    # 6: /Info metadata dictionary
    # 7: /Encrypt Standard Security Handler dictionary (if encrypted)
    next_obj_id = 8

    logo_obj_id: Optional[int] = None
    if logo_info:
        logo_obj_id = next_obj_id
        next_obj_id += 1

    page_obj_ids: List[int] = []

    for stream_text in page_streams:
        page_id = next_obj_id
        content_id = next_obj_id + 1
        next_obj_id += 2
        page_obj_ids.append(page_id)

        res_xobject = f" /XObject << /ImLogo {logo_obj_id} 0 R >>" if logo_obj_id else ""
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >>{res_xobject} >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects.append((page_id, page_dict))

        if encrypted and enc_key is not None:
            compressed = zlib.compress(stream_text.encode("latin-1", errors="replace"))
            encrypted_stream = _encrypt_pdf_object_bytes(enc_key, content_id, 0, compressed)
            content_obj = (
                f"<< /Length {len(encrypted_stream)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
                + encrypted_stream
                + b"\nendstream"
            )
        else:
            raw_stream = stream_text.encode("latin-1", errors="replace")
            content_obj = (
                f"<< /Length {len(raw_stream)} >>\nstream\n".encode("ascii")
                + raw_stream
                + b"\nendstream"
            )
        objects.append((content_id, content_obj))

    if logo_obj_id and logo_info:
        img_data = logo_info["data"]
        if encrypted and enc_key is not None:
            enc_img = _encrypt_pdf_object_bytes(enc_key, logo_obj_id, 0, img_data)
        else:
            enc_img = img_data
        img_dict = (
            f"<< /Type /XObject /Subtype /Image /Width {logo_info['width']} /Height {logo_info['height']} "
            f"{logo_info['colorspace']} /BitsPerComponent {logo_info['bits']} "
            f"{logo_info['filter']} /Length {len(enc_img)} >>\nstream\n".encode("ascii")
            + enc_img
            + b"\nendstream"
        )
        objects.append((logo_obj_id, img_dict))

    kids_refs = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((2, f"<< /Type /Pages /Kids [{kids_refs}] /Count {len(page_obj_ids)} >>".encode("ascii")))
    objects.append((3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))
    objects.append((4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"))
    objects.append((5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"))

    now_str = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    if encrypted and enc_key is not None:
        info_fields = {
            "/Title": title,
            "/Author": "Ephemres Enterprise Platform",
            "/Producer": "Ephemres Reporting Engine v3.3.2v",
            "/CreationDate": now_str,
            "/ModDate": now_str,
        }
        info_entries: List[str] = []
        for k, v in info_fields.items():
            enc_val = _encrypt_pdf_object_bytes(enc_key, 6, 0, v.encode("latin-1", errors="replace"))
            info_entries.append(f"{k} <{enc_val.hex()}>")
        info_dict = f"<< {' '.join(info_entries)} >>".encode("ascii")
        objects.append((6, info_dict))

        encrypt_dict = (
            f"<< /Filter /Standard /V 2 /R 3 /Length 128 /P {permissions} "
            f"/O <{owner_val.hex()}> /U <{user_val.hex()}> >>"
        ).encode("ascii")
        objects.append((7, encrypt_dict))
    else:
        info_dict = (
            f"<< /Title ({_pdf_escape_text(title)}) /Author (Ephemres Enterprise Platform) "
            f"/Producer (Ephemres Reporting Engine v3.3.2v) /CreationDate ({now_str}) /ModDate ({now_str}) >>"
        ).encode("ascii")
        objects.append((6, info_dict))

    objects.sort(key=lambda item: item[0])

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
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
    encrypt_ref = "/Encrypt 7 0 R " if (encrypted and enc_key is not None) else ""
    trailer = (
        f"trailer\n<< /Size {total_objs} /Root 1 0 R /Info 6 0 R {encrypt_ref}"
        f"/ID [<{id_hex}> <{id_hex}>] >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    buf.extend(trailer)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Pure-Python PDF 1.4 Inspector / Reader (Drop-in for pypdf.PdfReader in tests)
# ---------------------------------------------------------------------------
class _NativePdfPage:
    def __init__(self, text_content: str) -> None:
        self._text = text_content

    def extract_text(self) -> str:
        return self._text


class NativePdfReader:
    """
    Lightweight, zero-dependency PDF 1.4 reader and Standard Security Handler
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
                meta[k] = raw_bytes.decode("latin-1", errors="replace")
        self._metadata_cache = meta
        return meta

    @property
    def pages(self) -> List[_NativePdfPage]:
        if self._pages_cache is not None:
            return self._pages_cache
        key = self._ensure_key()
        extracted_pages: List[_NativePdfPage] = []
        for obj_id, obj_body in sorted(self._objects.items()):
            if b"/Type /Page" in obj_body and b"/Contents" in obj_body:
                m_c = re.search(rb"/Contents\s+(\d+)\s+0\s+R", obj_body)
                if not m_c:
                    continue
                c_id = int(m_c.group(1))
                c_body = self._objects.get(c_id, b"")
                m_stream = re.search(rb"stream\r?\n(.*?)\r?\nendstream", c_body, re.DOTALL)
                if not m_stream:
                    continue
                stream_bytes = m_stream.group(1)
                if key is not None:
                    stream_bytes = _encrypt_pdf_object_bytes(key, c_id, 0, stream_bytes)
                if b"/FlateDecode" in c_body:
                    stream_bytes = zlib.decompress(stream_bytes)
                content_str = stream_bytes.decode("latin-1", errors="replace")
                text_fragments: List[str] = []
                for m_tj in re.finditer(r"\(((?:\\.|[^\\)])*)\)\s*Tj", content_str):
                    frag = (
                        m_tj.group(1)
                        .replace("\\(", "(")
                        .replace("\\)", ")")
                        .replace("\\\\", "\\")
                    )
                    text_fragments.append(frag)
                extracted_pages.append(_NativePdfPage("\n".join(text_fragments)))
        self._pages_cache = extracted_pages
        return extracted_pages


def _render_pdf_sync(html_content: str, title: str, encrypted: bool = True) -> bytes:
    """
    Synchronous Zero-Dependency Native PDF 1.4 pipeline:
    1. Parses rendered HTML report and validates any embedded asset URIs via secure_url_fetcher.
    2. Typesets corporate header, KPI cards, section headers, and paginated tables into PDF 1.4 content streams.
    3. Enforces document immutability via ISO 32000-1 Standard Security Handler (128-bit RC4, read-only permissions)
       and stamps standard PDF metadata.
    """
    parser = _ReportHTMLParser()
    parser.feed(html_content)
    doc = parser.doc
    if title:
        doc.title = title

    logo_info: Optional[Dict[str, Any]] = None
    if doc.logo_bytes:
        logo_info = _extract_pdf_image_info(doc.logo_bytes, doc.logo_mime)

    page_streams = _typeset_document_pages(doc, logo_info=logo_info)
    return _compile_encrypted_pdf_1_4(
        page_streams=page_streams,
        title=doc.title,
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

