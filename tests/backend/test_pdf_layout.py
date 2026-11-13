"""Layout and accessibility guarantees of the native PDF typesetter (app.core.pdf).

These tests read the typesetter's own drawing operations, so they need no PDF tooling:
text never overlaps or leaves the page, table cells are never cut short, text meets
contrast and size floors, and the document is tagged for assistive technology.
"""
import random
import re
import types

import pytest

from app.core import pdf as P
from app.core.pdf_fonts import (
    FONT_BOLD,
    FONT_FALLBACK,
    FONT_REGULAR,
    embedded_font,
    shape,
    size_factor,
    text_width,
    to_winansi,
    wrap_text,
)
from app.routers.reports import jinja_env

LONG_HOST = "branch-ap-cafeteria-2nd-floor-east.clinical.northwind.example"
RCA = (
    "Direct Layer 2 Attachment (Same Subnet). No intermediate L3 routers detected. "
    "Failure isolated to local host, NIC, or local L2 switch port."
)

_TEXT_OP = re.compile(
    r"BT ([\d.]+ [\d.]+ [\d.]+) rg (/F\d) ([\d.]+) Tf ([-\d.]+) Tc 1 0 0 1 ([-\d.]+) ([-\d.]+) Tm "
    r"(?:\(((?:\\.|[^\\)])*)\)|<([0-9A-F]*)>) Tj ET"
)
CYRILLIC_HOST = "маршрутизатор-москва-01.северо-запад.example"



def _unescape(s):
    return s.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def _text_ops(page):
    out = []
    for op in page.ops:
        m = _TEXT_OP.fullmatch(op)
        if m:
            color, font, size, tc, x, y, literal, hexed = m.groups()
            nominal = float(size)
            if hexed is not None:
                ttf = embedded_font(font)
                txt = "".join(ttf.char_for(int(hexed[i: i + 4], 16)) for i in range(0, len(hexed), 4))
                nominal = float(size) / size_factor(font)  # drawn smaller to match Helvetica's x-height
            else:
                txt = _unescape(literal)
            out.append({
                "color": color, "font": font, "size": nominal, "x": float(x), "y": float(y), "text": txt,
                "w": text_width(txt, font, nominal, float(tc)),
            })
    return out


def _context(n_endpoints=40, n_incidents=25):
    endpoints = [
        {
            "hostname": (LONG_HOST if i % 4 == 0 else CYRILLIC_HOST if i % 4 == 1 else f"core-sw-{i:02d}"),
            "ip_address": f"2001:db8:4a1:{i:x}::{i * 3:x}" if i % 3 == 0 else f"10.{i % 4}.{i}.{i * 7 % 255}",
            "device_type": "ACCESS_POINT" if i % 5 == 0 else "ROUTER",
            "uptime_percentage": 99.9 - i * 0.01,
            "incident_count": i % 7,
            "formatted_downtime": "1d 3h 12m",
            "packet_loss_pct": 0.5,
            "avg_rtt_ms": 12.3,
            "operational_state": ["UP", "DOWN", "UNSTABLE", "PASSIVE"][i % 4],
        }
        for i in range(n_endpoints)
    ]
    incidents = [
        {
            "incident_id": f"{i:08X}", "hostname": LONG_HOST, "ip_address": "2001:db8:4a1:12::36",
            "start_time": "2026-10-01 00:14:22", "resolved_time": "Active / Ongoing",
            "duration": "1d 4h 2m", "trigger_reason": "Ping packet loss exceeded failure threshold",
            "rca_summary": RCA if i % 2 else None,
        }
        for i in range(n_incidents)
    ]
    interfaces = [
        {
            "router_hostname": LONG_HOST, "interface_idx": i, "interface_name": "Vlan120 - Clinical Imaging (PACS)",
            "is_wan": i == 1, "formatted_speed": "10 Gbps", "formatted_in_bps": "31.2 Mbps",
            "formatted_out_bps": "8.04 Mbps", "formatted_peak_bps": "912.7 Mbps",
            "utilization_pct": [None, 3.1, 64.2, 88.9][i % 4],
        }
        for i in range(20)
    ]
    return {
        "org": {"company_name": "Northwind Regional Health Network", "department": "Network Operations"},
        "report_type_label": "Comprehensive Master Operational Audit Dossier",
        "period_label": "2026-10-01 to 2026-10-31 UTC",
        "generated_at": "2026-11-09 14:22",
        "scope_title": "40 Infrastructure Targets",
        "endpoints": endpoints,
        "incidents": incidents,
        "summary": {"mean_uptime": "99.412", "total_incidents": 25, "total_downtime": "2d 14h 07m", "avg_rtt": "23.4"},
        "exporters": [{
            "hostname": LONG_HOST, "primary_ip": "10.0.0.2", "active_interfaces_count": 12,
            "formatted_ingress_bytes": "18.7 TB", "formatted_egress_bytes": "17.9 TB",
            "formatted_ingress_bps": "57.3 Mbps", "formatted_peak_bps": "9.41 Gbps",
        }],
        "interfaces": interfaces,
        "conversations": [{
            "src_display": f"{LONG_HOST} (10.2.40.17)", "dst_display": "2001:db8:ffff:1::443", "port_label": "443 / HTTPS",
            "router_name": LONG_HOST, "formatted_bytes": "1.92 TB", "pct_of_transit": 31.4,
        }] * 6,
        "qos_classes": [{"dscp": 46, "dscp_label": "EF (VoIP)", "formatted_bytes": "88.1 GB",
                         "formatted_packets": "402,118,551", "formatted_flows": "1,204,551", "percentage": 2.1}],
        "traffic_zones": [{"zone": "East-West (Internal)", "description": "Internal Mesh / Host-to-Host",
                           "formatted_bytes": "12.2 TB", "formatted_flows": "9,551,201", "percentage": 61.0}],
        "anomalies": {"total_count": 23, "syn_scan_count": 15, "syn_scan_bytes": "750 KB",
                      "rst_burst_count": 8, "rst_burst_bytes": "400 KB"},
        "wan_edge": {"interfaces_count": 2, "formatted_in_bytes": "4.21 TB", "formatted_out_bytes": "1.07 TB",
                     "formatted_in_bps": "12.9 Mbps", "formatted_out_bps": "3.3 Mbps"},
        "coverage_notes": ["Router and interface totals cover 2026-08-11 onward: this data is kept 90 days."],
    }


TEMPLATES = ["template_a_availability.html", "template_b_bandwidth.html", "template_c_master.html"]


def _typeset(template_name):
    html = jinja_env.get_template(template_name).render(**_context())
    parser = P._ReportHTMLParser()
    parser.feed(html)
    pages, root = P._Typesetter(parser.doc, None).run()
    return parser.doc, pages, root


def _luminance(rgb):
    def chan(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(float(v)) for v in rgb.split())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_text_stays_on_the_page_and_never_overlaps(template_name):
    _, pages, _ = _typeset(template_name)
    right = P._PAGE_W - P._MARGIN_X
    for page in pages:
        ops = _text_ops(page)
        assert ops
        for op in ops:
            assert op["x"] >= P._MARGIN_X - 0.01, op
            assert op["x"] + op["w"] <= right + 0.01, op
            assert op["y"] > 20.0, op
        # Text sharing a baseline (one table row line, or header left/right) must not collide.
        by_line = {}
        for op in ops:
            by_line.setdefault(round(op["y"], 1), []).append(op)
        for line in by_line.values():
            line.sort(key=lambda o: o["x"])
            for a, b in zip(line, line[1:]):
                assert a["x"] + a["w"] <= b["x"] + 0.01, (a["text"], b["text"])


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_table_cells_and_descriptions_are_never_cut_short(template_name):
    doc, pages, _ = _typeset(template_name)
    drawn = "".join(op["text"] for page in pages for op in _text_ops(page)).replace(" ", "")
    needles = [shape(doc.scope_desc, FONT_REGULAR)[0]]
    for kind, payload in doc.blocks:
        if kind == "table":
            needles += [shape(c.text.upper(), FONT_BOLD)[0] for c in payload.header]
            needles += [shape(c.text, FONT_REGULAR)[0] for row in payload.rows for c in row]
    for needle in needles:
        assert needle.replace(" ", "") in drawn, needle
    if template_name == "template_c_master.html":
        assert RCA.replace(" ", "") in drawn


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_text_meets_size_and_contrast_floors(template_name):
    _, pages, _ = _typeset(template_name)
    for page in pages:
        for op in _text_ops(page):
            assert op["size"] >= 7.0, op
            contrast = (1.05) / (_luminance(op["color"]) + 0.05)
            assert contrast >= 4.5, op


def test_numbers_right_align_and_states_are_not_colour_only():
    _, pages, _ = _typeset("template_b_bandwidth.html")
    ops = [op for page in pages for op in _text_ops(page)]
    right_edges = {round(op["x"] + op["w"], 1) for op in ops if op["text"] in ("31.2 Mbps",)}
    assert len(right_edges) == 1  # one column, one right edge
    high = [op for op in ops if op["text"] == "88.9%"]
    warn = [op for op in ops if op["text"] == "64.2%"]
    normal = [op for op in ops if op["text"] == "3.1%"]
    assert high and all(op["font"] == FONT_BOLD and op["color"] == P._DANGER for op in high)
    assert warn and all(op["font"] == FONT_BOLD for op in warn)
    assert normal and all(op["font"] == FONT_REGULAR for op in normal)


def _first_page_of(elem):
    for kid in elem.kids:
        if isinstance(kid, P._StructElem):
            page = _first_page_of(kid)
            if page is not None:
                return page
        else:
            return kid[0]
    return None


@pytest.mark.parametrize("filler_lines", range(0, 75, 3))
def test_heading_starts_on_the_same_page_as_its_table(filler_lines):
    doc = P._ParsedReportDocument(company_name="Acme")
    doc.blocks = [("paragraph", "Filler line of report text.")] * filler_lines
    rows = [[P._Cell(f"host-{i}"), P._Cell(RCA)] for i in range(8)]
    doc.blocks += [("heading", ("Outage ledger", True)), ("table", P._Table([P._Cell("Host"), P._Cell("Cause")], rows))]
    _, root = P._Typesetter(doc, None).run()
    heading = next(e for e in root.kids if e.role == "H2")
    table = next(e for e in root.kids if e.role == "Table")
    tbody = next(e for e in table.kids if e.role == "TBody")
    start = _first_page_of(heading)
    assert _first_page_of(table) == start
    assert _first_page_of(tbody.kids[0]) == start and _first_page_of(tbody.kids[1]) == start


def test_long_table_repeats_its_header_as_a_pagination_artifact():
    doc = P._ParsedReportDocument(company_name="Acme", period_label="Period: 2026-10-01 to 2026-10-31 UTC")
    rows = [[P._Cell(f"host-{i}"), P._Cell("99.9%", align="right")] for i in range(150)]
    doc.blocks = [("table", P._Table([P._Cell("Host"), P._Cell("Uptime", align="right")], rows))]
    pages, root = P._Typesetter(doc, None).run()
    assert len(pages) >= 3
    table = next(e for e in root.kids if e.role == "Table")
    assert [e.role for e in table.kids] == ["THead", "TBody"]
    assert len(table.kids[0].kids) == 1 and len(table.kids[1].kids) == 150
    for page in pages[1:]:
        joined = "\n".join(page.ops)
        assert "/Artifact << /Type /Pagination >> BDC" in joined
        assert "(HOST) Tj" in joined


def test_tagged_pdf_catalog_language_title_and_alt_text():
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
    )
    html = jinja_env.get_template("template_a_availability.html").render(
        **dict(_context(4, 1), org={"company_name": "Acme", "logo_data": "data:image/png;base64," + base64.b64encode(png).decode()})
    )
    pdf = P._render_pdf_sync(html, "Availability Report", encrypted=False)
    assert pdf.startswith(b"%PDF-1.5")
    assert b"/MarkInfo << /Marked true >>" in pdf
    assert b"/StructTreeRoot" in pdf and b"/ParentTree" in pdf
    assert b"/ViewerPreferences << /DisplayDocTitle true >>" in pdf
    assert b"/Lang <" + b"en".hex().encode() + b">" in pdf
    assert pdf.count(b"/S /TH ") == 9 + 6  # endpoint matrix + incident ledger header cells
    assert b"/Tabs /S" in pdf
    assert re.search(rb"/S /Figure .*?/Alt <([0-9a-f]+)>", pdf)
    alt_hex = re.search(rb"/S /Figure .*?/Alt <([0-9a-f]+)>", pdf).group(1)
    assert bytes.fromhex(alt_hex.decode()) == b"Acme"
    # Every content-stream drawing sits in tagged content or an artifact.
    reader = P.NativePdfReader(pdf)
    assert reader.metadata["/Title"] == "Availability Report"


def test_encrypted_reports_stay_read_only_but_allow_accessibility_extraction():
    pdf = P._render_pdf_sync("<html><body><p>99.98% availability</p></body></html>", "t", encrypted=True)
    perms = int(re.search(rb"/P (-?\d+)", pdf).group(1)) & 0xFFFFFFFF
    assert perms & (1 << 9)  # bit 10: extract text and graphics for accessibility
    assert perms & (1 << 2)  # bit 3: print
    assert not perms & (1 << 3)  # bit 4: modify
    assert not perms & (1 << 4)  # bit 5: copy
    assert not perms & (1 << 5)  # bit 6: annotate
    reader = P.NativePdfReader(pdf)
    assert "99.98% availability" in reader.pages[0].extract_text()


def test_uppercased_wrapped_and_non_latin_text_carries_actual_text():
    doc = P._ParsedReportDocument(company_name="Acme")
    doc.blocks = [
        ("heading", ("Device Matrix", True)),
        ("table", P._Table([P._Cell("Host")], [[P._Cell("東京-core-sw ≥ 80%")], [P._Cell(RCA)]])),
    ]
    pages, _ = P._Typesetter(doc, None).run()
    stream = "\n".join(op for page in pages for op in page.ops)
    for original in ("Device Matrix", "東京-core-sw ≥ 80%", "Host"):
        assert P._utf16_hex(original) in stream, original
    # No bundled font has CJK glyphs: those print as "?", the rest (≥) in the embedded font.
    drawn = [op["text"] for page in pages for op in _text_ops(page) if op["font"] == FONT_FALLBACK]
    assert "??-core-sw ≥ 80%" in drawn


def test_non_latin_text_is_drawn_in_an_embedded_subset_font_and_reads_back():
    html = (
        "<html><body><h2>Москва — ≥ 80% → Ελλάδα</h2><table><thead><tr><th>Узел</th><th>State</th></tr></thead>"
        f"<tbody><tr><td>{CYRILLIC_HOST}</td><td>UP</td></tr></tbody></table></body></html>"
    )
    for encrypted in (False, True):
        pdf = P._render_pdf_sync(html, "Отчёт", encrypted=encrypted)
        text = P.NativePdfReader(pdf).pages[0].extract_text().split("\n")
        assert "Москва — ≥ 80% → Ελλάδα" in text
        assert "УЗЕЛ" in text and "STATE" in text
        assert CYRILLIC_HOST in "".join(text)
        assert P.NativePdfReader(pdf).metadata["/Title"] == "Отчёт"
        if not encrypted:
            assert pdf.count(b"/Subtype /Type0") == 2  # bold (heading, header) and regular (cell)
            assert b"/Encoding /Identity-H" in pdf and b"/CIDToGIDMap /Identity" in pdf
            assert pdf.count(b"/FontFile2") == 2 and pdf.count(b"/ToUnicode") == 2
            assert re.search(rb"/BaseFont /[A-Z]{6}\+DejaVuSans-Bold", pdf)
            assert len(pdf) < 60_000  # subsets, not the 1.4 MB of font files


def test_reports_without_non_latin_text_embed_no_font():
    pdf = P._render_pdf_sync("<html><body><p>core-sw-Zürich — 99.98% · “ok”</p></body></html>", "t", encrypted=False)
    assert b"/FontFile2" not in pdf and b"/Type0" not in pdf


def test_font_subset_keeps_used_glyphs_and_composite_components():
    import struct

    from app.core.pdf_ttf import _checksum

    ttf = embedded_font(FONT_FALLBACK)
    composite = next(
        g for g in sorted(set(ttf.cmap.values()))
        if len(ttf._glyph_data(g)) >= 10 and struct.unpack(">h", ttf._glyph_data(g)[:2])[0] < 0
    )
    components = ttf._components(composite)
    assert components
    used = [ttf.glyph(c) for c in "Москва"] + [composite]
    keep = ttf.closure(used)
    assert set(used) | set(components) | {0} <= keep
    program = ttf.subset(used)
    assert _checksum(program) == 0xB1B0AFBA  # whole-font checksum with checkSumAdjustment applied
    num_tables = struct.unpack(">H", program[4:6])[0]
    tags = {program[12 + 16 * i: 16 + 16 * i] for i in range(num_tables)}
    assert tags == {b"cvt ", b"fpgm", b"glyf", b"head", b"hhea", b"hmtx", b"loca", b"maxp", b"prep"}
    assert len(program) < len(ttf.data) / 8


def test_typographic_characters_survive_and_read_back():
    pdf = P._render_pdf_sync(
        "<html><head><style>@page { @bottom-left { content: \"Confidential — Internal\"; } }</style></head>"
        "<body><p>3 exporters · 16 interfaces — “quoted”</p></body></html>",
        "t",
        encrypted=True,
    )
    text = P.NativePdfReader(pdf).pages[0].extract_text()
    assert "3 exporters · 16 interfaces — “quoted”" in text
    assert "Confidential — Internal" in text


@pytest.mark.parametrize("data", [b"ab\r", b"ab\r\n", b"\r", b"ab"])
def test_reader_takes_stream_data_by_length_not_by_end_of_line(data):
    """Stream data that ends in CR or CRLF keeps those bytes: only the marker before
    endstream is dropped. The old delimiter match cut a trailing 0x0D off the data."""
    pdf = (
        b"%PDF-1.5\n1 0 obj\n<< /Length " + str(len(data)).encode() + b" >>\nstream\n"
        + data + b"\nendstream\nendobj\n"
    )
    assert P.NativePdfReader(pdf)._stream(1) == data


def test_encrypted_stream_ending_in_carriage_return_reads_back(monkeypatch):
    """The flake behind test_typographic_characters_survive_and_read_back (1 run in ~150):
    random file IDs and owner passwords change the RC4 key, so 1 page stream in 256 ends in
    0x0D. Seeded passwords and IDs make that case happen in every run, in the same place
    (the 14th render with this seed; if layout changes move it, the loop still finds one)."""
    seeded = random.Random(20261132)
    monkeypatch.setattr(
        P, "secrets", types.SimpleNamespace(token_hex=lambda n: seeded.randbytes(n).hex())
    )
    html = "<html><body><p>3 exporters · 16 interfaces — “quoted”</p></body></html>"
    for _ in range(3000):
        pdf = P._render_pdf_sync(html, "t", encrypted=True)
        content_id = re.search(rb"/Contents (\d+) 0 R", pdf).group(1)
        m = re.search(rb"\n" + content_id + rb" 0 obj\n<< /Length (\d+)[^>]*>>\nstream\n", pdf)
        if pdf[m.end() + int(m.group(1)) - 1] == 0x0D:
            break
    else:
        pytest.fail("no seeded render put 0x0D at the end of the page stream")
    assert "3 exporters · 16 interfaces — “quoted”" in P.NativePdfReader(pdf).pages[0].extract_text()


def test_wrap_text_breaks_identifiers_at_separators_and_keeps_every_character():
    enc = to_winansi(f"{LONG_HOST} (2001:db8:4a1:12::36)")
    lines = wrap_text(enc, FONT_REGULAR, 7.5, 60.0)
    assert len(lines) > 2
    assert "".join(lines).replace(" ", "") == enc.replace(" ", "")
    assert all(text_width(line, FONT_REGULAR, 7.5) <= 60.01 for line in lines)
    assert lines[0].endswith((".", "-", ":"))


@pytest.mark.parametrize("prose_text", [RCA, RCA + " " + RCA, "Ping packet loss exceeded failure threshold " * 3])
def test_column_fit_gives_prose_the_room_and_unwraps_dates_only_when_free(prose_text):
    """An incident ledger: fixed host column, two date columns, one long root-cause column.
    Compare with brute force over which date columns stay on one line: the fit must give the
    prose its fewest lines, then keep as many dates on one line as that allows."""
    pad = 2 * P._CELL_PAD_X
    host = P._measure_cell(to_winansi("esx-host-07.cluster-b.northwind.example"), FONT_REGULAR, 7.5)
    date = P._measure_cell(to_winansi("2026-10-01 00:14:22"), FONT_REGULAR, 7.5)
    prose = P._measure_cell(to_winansi(prose_text), FONT_REGULAR, 7.5)
    day = max(date.words) + pad
    widths = P._fit_columns(
        mins=[host.total + pad, day, day, 60.0],
        maxs=[host.total + pad, date.total + pad, date.total + pad, prose.total + pad],
        total=P._CONTENT_W,
        cells=[[host, date, date, prose]] * 10,
        weights=[1.0] * 10,
        pad=pad,
    )
    assert abs(sum(widths) - P._CONTENT_W) < 0.01

    def prose_lines(width):
        return P._count_lines(prose, width - pad)

    options = []
    for unwrapped in (0, 1, 2):
        rest = P._CONTENT_W - (host.total + pad) - unwrapped * (date.total + pad) - (2 - unwrapped) * day
        options.append((prose_lines(min(rest, prose.total + pad)), -unwrapped))
    best_lines, best_unwrapped = min(options)
    assert prose_lines(widths[3]) == best_lines
    assert sum(w >= date.total + pad - 0.01 for w in widths[1:3]) == -best_unwrapped


def test_count_lines_matches_wrap_text():
    import random
    rng = random.Random(5)
    vocab = [LONG_HOST, "Transit", "failure", "at", "Hop", "4", "(203.0.113.9).", "2026-10-01", "00:14:22",
             "2001:db8:4a1:12::36", "Vlan120", "-", "Clinical", "Imaging", "(PACS)"]
    for _ in range(300):
        enc = to_winansi(" ".join(rng.choice(vocab) for _ in range(rng.randint(1, 14))))
        m = P._measure_cell(enc, FONT_REGULAR, 7.5)
        # Exact whenever no single identifier segment is wider than the column, which the
        # fitter guarantees unless the table cannot fit at all.
        width = rng.uniform(max(max(seg) for seg in m.segments), 250.0)
        assert P._count_lines(m, width) == len(wrap_text(enc, FONT_REGULAR, 7.5, width)), (enc, width)


def test_fitted_tables_are_never_taller_than_simple_layouts():
    import random
    rng = random.Random(3)
    vocab = ("core sw edge fw transit failure at hop path confirmed operational through router link down "
             "2026-10-01 00:14:22 443 / HTTPS 10.2.40.17 dc1-core-rtr-01.mgmt.northwind.example").split()
    typesetter = P._Typesetter(P._ParsedReportDocument(), None)

    def height(tbl, widths):
        total = 0.0
        for row in tbl.rows:
            total += max(len(wrap_text(to_winansi(c.text), FONT_REGULAR, 7.5, w - 2 * P._CELL_PAD_X)) for c, w in zip(row, widths))
        return total

    compared = 0
    for _ in range(60):
        ncols = rng.randint(3, 7)
        lens = [rng.choice([1, 2, 3, 6, 12]) for _ in range(ncols)]
        rows = [[P._Cell(" ".join(rng.choice(vocab) for _ in range(max(1, int(rng.gauss(n, n / 3))))))
                 for n in lens] for _ in range(rng.randint(3, 20))]
        tbl = P._Table([P._Cell(f"Col {i}") for i in range(ncols)], rows)
        fitted = typesetter._prepare_table(tbl).widths
        # Simple layouts under the same rule the fitter keeps: never break inside a word.
        cells = [[P._measure_cell(to_winansi(r[i].text), FONT_REGULAR, 7.5) for i in range(ncols)] for r in rows]
        maxs = [max(c[i].total for c in cells) + 10 for i in range(ncols)]
        mins = [max(max(c[i].words) for c in cells) + 10 for i in range(ncols)]
        if sum(mins) > P._CONTENT_W:
            continue
        proportional = P._clamped_share(maxs, mins, P._CONTENT_W)
        equal = P._clamped_share([1.0] * ncols, mins, P._CONTENT_W)
        assert height(tbl, fitted) <= height(tbl, proportional)
        assert height(tbl, fitted) <= height(tbl, equal)
        compared += 1
    assert compared >= 20


def test_long_paragraph_flows_across_pages_without_losing_text():
    words = [f"word{i}" for i in range(2500)]
    doc = P._ParsedReportDocument(company_name="Acme")
    doc.blocks = [("paragraph", " ".join(words)), ("heading", ("After", False))]
    pages, root = P._Typesetter(doc, None).run()
    assert len(pages) >= 2
    drawn = " ".join(op["text"] for page in pages for op in _text_ops(page))
    assert all(f" {w} " in f" {drawn} " for w in words[:: 97] + words[-3:])
    paragraph = next(e for e in root.kids if e.role == "P" and len(e.kids) > 1)
    assert len({page for page, _ in paragraph.kids}) == len(paragraph.kids)  # one marked section per page
    for page in pages:
        assert all(op["y"] > P._CONTENT_BOTTOM - 12 for op in _text_ops(page) if op["text"].startswith("word"))


def test_twin_timestamp_columns_get_the_same_width():
    rows = [
        [P._Cell(f"#{i:08X}"), P._Cell(LONG_HOST, bold=True), P._Cell("2026-11-07 23:50:21"),
         P._Cell("Active / Ongoing" if i == 0 else "2026-11-07 23:53:21"), P._Cell("3m", align="right"), P._Cell(RCA)]
        for i in range(12)
    ]
    header = [P._Cell(h) for h in ("Incident ID", "Target", "Start (UTC)", "Resolved (UTC)", "Duration", "Root cause")]
    header[4].align = "right"
    widths = P._Typesetter(P._ParsedReportDocument(), None)._prepare_table(P._Table(header, rows)).widths
    assert abs(widths[2] - widths[3]) < 0.01


@pytest.mark.parametrize("filler_lines", range(0, 80, 2))
def test_last_table_row_never_stands_alone_on_a_page(filler_lines):
    doc = P._ParsedReportDocument(company_name="Acme")
    doc.blocks = [("paragraph", "Filler line of report text.")] * filler_lines
    rows = [[P._Cell(f"host-{i}"), P._Cell("99.9%", align="right")] for i in range(30)]
    doc.blocks += [("table", P._Table([P._Cell("Host"), P._Cell("Uptime", align="right")], rows))]
    _, root = P._Typesetter(doc, None).run()
    table = next(e for e in root.kids if e.role == "Table")
    trs = next(e for e in table.kids if e.role == "TBody").kids
    last_page = _first_page_of(trs[-1])
    assert sum(1 for tr in trs if _first_page_of(tr) == last_page) >= 2
