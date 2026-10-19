"""
Compatibility re-export shim for the Ephemres PDF reporting engine.
The native zero-dependency PDF 1.4 compilation implementation resides in app.core.pdf,
and report data collectors reside in app.services.report_compiler.
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
from app.services.report_compiler import (
    compile_availability_report_data,
    compile_bandwidth_report_data,
    compile_master_report_data,
)

__all__ = [
    "HTML",
    "FetchedResource",
    "NativePdfReader",
    "PDF_READONLY_PERMISSIONS",
    "_render_pdf_sync",
    "render_pdf_async",
    "secure_url_fetcher",
    "compile_availability_report_data",
    "compile_bandwidth_report_data",
    "compile_master_report_data",
]
