"""Zero-dependency TrueType reader and subsetter for embedding fonts in report PDFs.

Reads what a PDF needs from a TrueType (glyf-outline) font: the character map, advance
widths, vertical metrics and the PostScript name. subset() writes a font program that
keeps only the glyphs a document uses (plus any components of composite glyphs). Glyph
IDs are left unchanged, so the PDF can use an Identity CIDToGIDMap.
"""
from __future__ import annotations

import struct
from typing import Dict, Iterable, List, Set, Tuple

# Tables a CIDFontType2 font program needs (ISO 32000-1, 9.9): outlines, metrics, hinting.
_SUBSET_TABLES = (b"cvt ", b"fpgm", b"glyf", b"head", b"hhea", b"hmtx", b"loca", b"maxp", b"prep")

# Composite glyph component flags (OpenType 'glyf' table)
_ARG_1_AND_2_ARE_WORDS = 0x0001
_WE_HAVE_A_SCALE = 0x0008
_MORE_COMPONENTS = 0x0020
_WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
_WE_HAVE_A_TWO_BY_TWO = 0x0080


def _checksum(data: bytes) -> int:
    padded = data + b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(padded) // 4}I", padded)) & 0xFFFFFFFF


class TrueTypeFont:
    def __init__(self, data: bytes) -> None:
        self.data = data
        if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
            raise ValueError("Not a TrueType (glyf) font.")
        num_tables = struct.unpack(">H", data[4:6])[0]
        self.tables: Dict[bytes, Tuple[int, int]] = {}
        for i in range(num_tables):
            tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            self.tables[tag] = (offset, length)

        head = self._table(b"head")
        self.units_per_em = struct.unpack(">H", head[18:20])[0]
        self.bbox = struct.unpack(">4h", head[36:44])
        self.long_loca = struct.unpack(">h", head[50:52])[0] == 1

        hhea = self._table(b"hhea")
        self.ascent, self.descent = struct.unpack(">hh", hhea[4:8])
        num_h_metrics = struct.unpack(">H", hhea[34:36])[0]
        self.num_glyphs = struct.unpack(">H", self._table(b"maxp")[4:6])[0]

        hmtx = self._table(b"hmtx")
        advances = [struct.unpack(">H", hmtx[4 * i: 4 * i + 2])[0] for i in range(num_h_metrics)]
        self.advances = advances + [advances[-1]] * (self.num_glyphs - num_h_metrics)

        self.cap_height = int(self.ascent * 0.7)
        self.x_height = int(self.units_per_em * 0.5)
        if b"OS/2" in self.tables:
            os2 = self._table(b"OS/2")
            if struct.unpack(">H", os2[0:2])[0] >= 2 and len(os2) >= 90:
                self.x_height, self.cap_height = struct.unpack(">hh", os2[86:90])
        self.italic_angle = 0.0
        if b"post" in self.tables:
            post = self._table(b"post")
            self.italic_angle = struct.unpack(">i", post[4:8])[0] / 65536.0

        self.cmap = self._read_cmap()
        self.postscript_name = self._read_postscript_name()

        loca = self._table(b"loca")
        if self.long_loca:
            self.loca = list(struct.unpack(f">{self.num_glyphs + 1}I", loca[: 4 * (self.num_glyphs + 1)]))
        else:
            self.loca = [2 * v for v in struct.unpack(f">{self.num_glyphs + 1}H", loca[: 2 * (self.num_glyphs + 1)])]

        # OS/2 heights are often inaccurate (DejaVu's sxHeight is 1024, its 'x' is 1120 tall):
        # measure the outlines of 'x' and 'H' instead when the font has them.
        self.x_height = self._glyph_top("x") or self.x_height
        self.cap_height = self._glyph_top("H") or self.cap_height

    @classmethod
    def from_file(cls, path: str) -> "TrueTypeFont":
        with open(path, "rb") as f:
            return cls(f.read())

    def _table(self, tag: bytes) -> bytes:
        offset, length = self.tables[tag]
        return self.data[offset: offset + length]

    def _read_cmap(self) -> Dict[int, int]:
        cmap = self._table(b"cmap")
        num = struct.unpack(">H", cmap[2:4])[0]
        subtables = {}
        for i in range(num):
            platform, encoding, offset = struct.unpack(">HHI", cmap[4 + 8 * i: 12 + 8 * i])
            subtables[(platform, encoding)] = offset
        for key in ((3, 10), (0, 4), (3, 1), (0, 3)):
            if key not in subtables:
                continue
            off = subtables[key]
            fmt = struct.unpack(">H", cmap[off: off + 2])[0]
            if fmt == 12:
                return self._cmap_format12(cmap, off)
            if fmt == 4:
                return self._cmap_format4(cmap, off)
        raise ValueError("No Unicode cmap (format 4 or 12) in font.")

    @staticmethod
    def _cmap_format4(cmap: bytes, off: int) -> Dict[int, int]:
        seg_count = struct.unpack(">H", cmap[off + 6: off + 8])[0] // 2
        ends = struct.unpack(f">{seg_count}H", cmap[off + 14: off + 14 + 2 * seg_count])
        p = off + 16 + 2 * seg_count
        starts = struct.unpack(f">{seg_count}H", cmap[p: p + 2 * seg_count])
        p += 2 * seg_count
        deltas = struct.unpack(f">{seg_count}h", cmap[p: p + 2 * seg_count])
        p += 2 * seg_count
        range_offsets_pos = p
        range_offsets = struct.unpack(f">{seg_count}H", cmap[p: p + 2 * seg_count])
        out: Dict[int, int] = {}
        for i in range(seg_count):
            for code in range(starts[i], ends[i] + 1):
                if code == 0xFFFF:
                    continue
                if range_offsets[i] == 0:
                    gid = (code + deltas[i]) & 0xFFFF
                else:
                    pos = range_offsets_pos + 2 * i + range_offsets[i] + 2 * (code - starts[i])
                    gid = struct.unpack(">H", cmap[pos: pos + 2])[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    out[code] = gid
        return out

    @staticmethod
    def _cmap_format12(cmap: bytes, off: int) -> Dict[int, int]:
        groups = struct.unpack(">I", cmap[off + 12: off + 16])[0]
        out: Dict[int, int] = {}
        for g in range(groups):
            start, end, gid = struct.unpack(">III", cmap[off + 16 + 12 * g: off + 28 + 12 * g])
            for code in range(start, end + 1):
                out[code] = gid + code - start
        return out

    def _read_postscript_name(self) -> str:
        if b"name" not in self.tables:
            return "EmbeddedFont"
        name = self._table(b"name")
        count, string_offset = struct.unpack(">HH", name[2:6])
        for i in range(count):
            platform, encoding, _, name_id, length, offset = struct.unpack(">6H", name[6 + 12 * i: 18 + 12 * i])
            if name_id != 6:
                continue
            raw = name[string_offset + offset: string_offset + offset + length]
            text = raw.decode("utf-16-be", errors="ignore") if platform in (0, 3) else raw.decode("latin-1")
            clean = "".join(c for c in text if c.isalnum() or c in "-_")
            if clean:
                return clean
        return "EmbeddedFont"

    def _glyph_top(self, ch: str) -> int:
        """yMax of a character's outline, or 0 if the font lacks it."""
        gid = self.cmap.get(ord(ch), 0)
        data = self._glyph_data(gid) if gid else b""
        return struct.unpack(">h", data[8:10])[0] if len(data) >= 10 else 0

    # -- metrics ---------------------------------------------------------------
    def has_char(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def glyph(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def char_for(self, gid: int) -> str:
        """The character a glyph draws (the lowest code point mapped to it), for /ToUnicode."""
        if not hasattr(self, "_reverse"):
            reverse: Dict[int, int] = {}
            for code, g in sorted(self.cmap.items()):
                reverse.setdefault(g, code)
            self._reverse = reverse
        code = self._reverse.get(gid)
        return chr(code) if code is not None else "\ufffd"

    def advance_1000(self, gid: int) -> float:
        """Advance width in 1/1000 em, the unit PDF font widths use."""
        return self.advances[gid] * 1000.0 / self.units_per_em

    def scale(self, value: float) -> int:
        return int(round(value * 1000.0 / self.units_per_em))

    # -- subsetting ------------------------------------------------------------
    def _glyph_data(self, gid: int) -> bytes:
        glyf_off = self.tables[b"glyf"][0]
        return self.data[glyf_off + self.loca[gid]: glyf_off + self.loca[gid + 1]]

    def _components(self, gid: int) -> List[int]:
        data = self._glyph_data(gid)
        if len(data) < 10 or struct.unpack(">h", data[0:2])[0] >= 0:
            return []
        out = []
        p = 10
        while True:
            flags, component = struct.unpack(">HH", data[p: p + 4])
            out.append(component)
            p += 4
            p += 4 if flags & _ARG_1_AND_2_ARE_WORDS else 2
            if flags & _WE_HAVE_A_SCALE:
                p += 2
            elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
                p += 4
            elif flags & _WE_HAVE_A_TWO_BY_TWO:
                p += 8
            if not flags & _MORE_COMPONENTS:
                return out

    def closure(self, gids: Iterable[int]) -> Set[int]:
        """The glyphs to keep: the requested ones, .notdef, and every composite component."""
        keep = {0}
        todo = [g for g in gids if 0 <= g < self.num_glyphs]
        while todo:
            gid = todo.pop()
            if gid in keep and gid != 0:
                continue
            keep.add(gid)
            todo.extend(c for c in self._components(gid) if c not in keep)
        return keep

    def subset(self, gids: Iterable[int]) -> bytes:
        """A font program holding only the given glyphs, with glyph IDs unchanged."""
        keep = self.closure(gids)
        glyf = bytearray()
        loca = [0]
        for gid in range(self.num_glyphs):
            if gid in keep:
                glyf.extend(self._glyph_data(gid))
                glyf.extend(b"\0" * (-len(glyf) % 4))
            loca.append(len(glyf))

        head = bytearray(self._table(b"head"))
        head[8:12] = b"\0\0\0\0"  # checkSumAdjustment, set below
        head[50:52] = struct.pack(">h", 1)  # long loca offsets

        tables = {
            b"head": bytes(head),
            b"glyf": bytes(glyf),
            b"loca": struct.pack(f">{len(loca)}I", *loca),
        }
        for tag in (b"cvt ", b"fpgm", b"hhea", b"hmtx", b"maxp", b"prep"):
            if tag in self.tables:
                tables[tag] = self._table(tag)

        tags = sorted(t for t in _SUBSET_TABLES if t in tables)
        num = len(tags)
        entry_selector = max(num.bit_length() - 1, 0)
        search_range = (1 << entry_selector) * 16
        header = struct.pack(">IHHHH", 0x00010000, num, search_range, entry_selector, num * 16 - search_range)
        offset = 12 + 16 * num
        directory = bytearray()
        body = bytearray()
        for tag in tags:
            data = tables[tag]
            directory.extend(struct.pack(">4sIII", tag, _checksum(data), offset + len(body), len(data)))
            body.extend(data)
            body.extend(b"\0" * (-len(body) % 4))
        font = bytearray(header + directory + body)
        adjustment = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
        head_offset = offset + sum(len(tables[t]) + (-len(tables[t]) % 4) for t in tags[: tags.index(b"head")])
        font[head_offset + 8: head_offset + 12] = struct.pack(">I", adjustment)
        return bytes(font)
