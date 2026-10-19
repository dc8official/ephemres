"""
Compatibility re-export shim for the Ephemres PDF reporting engine.
The native zero-dependency PDF 1.4 compilation implementation resides in app.core.pdf.
"""
from app.core.pdf import (
    HTML,
    FetchedResource,
    NativePdfReader,
    PDF_READONLY_PERMISSIONS,
    _render_pdf_sync,
    render_pdf_async,
    secure_url_fetcher,
)

__all__ = [
    "HTML",
    "FetchedResource",
    "NativePdfReader",
    "PDF_READONLY_PERMISSIONS",
    "_render_pdf_sync",
    "render_pdf_async",
    "secure_url_fetcher",
]
