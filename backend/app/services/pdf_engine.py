from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import pypdf
from pypdf.constants import UserAccessPermissions
import weasyprint
import weasyprint.urls
from weasyprint import HTML

logger = logging.getLogger(__name__)

ALLOWED_ASSET_DIRS = (
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates", "reports")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "assets")),
)

_base_fetcher = weasyprint.urls.URLFetcher(allowed_protocols=("data", "file"))


def secure_url_fetcher(url: str, timeout: int = 10, ssl_context: Any = None) -> Any:
    """
    Air-gapped, SSRF/LFI-hardened URL fetcher for WeasyPrint.
    - Permits inline data: URIs (e.g. Base64 logos).
    - Blocks all external network schemas (http, https, ftp).
    - Restricts local file:// paths strictly to authorized template and asset directories.
    """
    if not url:
        raise ValueError("Empty URL cannot be fetched.")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "data":
        return _base_fetcher(url)

    if scheme in ("http", "https", "ftp"):
        logger.warning("SSRF blocked: attempted external fetch of '%s'", url)
        raise PermissionError(f"External network access is prohibited during report compilation: {scheme}://")

    # Handle local filesystem paths
    local_path = parsed.path if scheme == "file" else url
    abs_path = os.path.abspath(local_path)

    is_allowed = any(abs_path.startswith(allowed_dir) for allowed_dir in ALLOWED_ASSET_DIRS)
    if not is_allowed or not os.path.exists(abs_path):
        logger.warning("LFI blocked: attempted access to unauthorized local path '%s'", abs_path)
        raise PermissionError(f"Access to file path '{local_path}' is denied: not within authorized asset roots.")

    return _base_fetcher(f"file://{abs_path}")


# WeasyPrint requires _fail_on_errors attribute on URL fetcher to handle blocked resources gracefully
secure_url_fetcher._fail_on_errors = False


def _render_pdf_sync(html_content: str, title: str) -> bytes:
    """
    Synchronous WeasyPrint + pypdf pipeline:
    1. Compiles HTML/CSS to PDF with secure URL fetcher.
    2. Enforces document immutability via pypdf permission encryption (read-only).
    3. Stamps standard PDF metadata.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates", "reports"))
    
    html = HTML(
        string=html_content,
        base_url=base_dir,
        url_fetcher=secure_url_fetcher,
    )
    raw_pdf_bytes = html.write_pdf(
        stylesheets=[],
        presentational_hints=True,
    )

    # Apply pypdf immutability and metadata
    reader = pypdf.PdfReader(io.BytesIO(raw_pdf_bytes))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    # Document metadata
    now_str = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    writer.add_metadata({
        "/Title": title,
        "/Author": "Ephemres Enterprise Platform",
        "/Producer": "Ephemres Reporting Engine v3.3.1n",
        "/CreationDate": now_str,
        "/ModDate": now_str,
    })

    # Restrict permissions: allow print, prohibit modification and content extraction
    permissions = UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION

    # Encrypt with random owner password and empty user password (opens seamlessly in viewers)
    owner_pwd = secrets.token_hex(32)
    writer.encrypt(
        user_password="",
        owner_password=owner_pwd,
        permissions_flag=permissions,
    )

    output_stream = io.BytesIO()
    writer.write(output_stream)
    return output_stream.getvalue()


async def render_pdf_async(html_content: str, title: str = "Ephemres Network Telemetry Report") -> bytes:
    """
    Asynchronously executes PDF rendering offloaded to a worker thread.
    Guarantees that CPU-bound rasterization never starves the FastAPI event loop.
    Enforces a strict 60-second timeout.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_render_pdf_sync, html_content, title),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        logger.error("PDF generation timed out after 60 seconds")
        raise TimeoutError("PDF report rendering exceeded maximum execution threshold (60s).")
