#!/usr/bin/env python3
"""
AK820 Pro LCD asset converter.

Decodes the source PNGs into flat RGB565 .raw pixel files plus a manifest, and
(with --flash) packs them into a single flash_assets.bin image + a flash_assets.h
id header for provisioning into the keyboard's external SPI flash with
`ak820ctl flash write`. No third-party deps: PNG is decoded here with stdlib zlib
only (Pillow/ImageMagick are not available).

Pixels are RGB565, alpha composited over black. raw/ stores them big-endian
(hi byte first) -- the order the CPU/RAM draw path (lcd_blit_ram -> tx_pixels)
wants. --flash byte-swaps to lo-byte-first, which is what the flash->LCD DMA needs:
it streams flash bytes through a 16-bit SPI transfer that shifts each pair out
MSB first, so on-flash data must already be lo-first to arrive correctly. The DMA
only streams raw pixels -- it cannot expand 1bpp or blend fg/bg on the fly -- so the
on-flash bytes must already be exactly what the panel consumes. Glyph colours are
therefore baked; harmless here, as the dashboard is uniformly white on black.

Font atlases are self-describing: a magenta (255,0,255) marker sits at each glyph
cell's top-left corner, so marker spacing IS the advance and marker count IS the
glyph count. The markers are metadata and resolve to background in the output.

Usage:
    python3 mkraw.py            # inspect only: report what each PNG contains
    python3 mkraw.py --write    # also emit raw/<name>.raw + raw/manifest.json
    python3 mkraw.py --flash    # also pack flash_assets.bin + .h (implies --write)
"""

import json
import struct
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "raw")

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}  # PNG colour type -> samples per pixel


# --------------------------------------------------------------------------- PNG
def png_chunks(blob):
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i = 8
    while i < len(blob):
        ln = int.from_bytes(blob[i:i + 4], "big")
        typ = blob[i + 4:i + 8]
        data = blob[i + 8:i + 8 + ln]
        yield typ, data
        i += 8 + ln + 4  # skip CRC


def unfilter(raw, h, stride, bpp):
    """Reverse the per-scanline PNG filters. Operates on bytes, not pixels."""
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        ft = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if ft == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        elif ft != 0:
            raise ValueError("bad filter type %d" % ft)
        out += line
        prev = line
    return out


def samples(line, w, nch, depth):
    """Yield per-pixel sample tuples from one unfiltered scanline."""
    if depth == 8:
        for x in range(w):
            yield tuple(line[x * nch + c] for c in range(nch))
    elif depth == 16:
        for x in range(w):
            yield tuple(line[(x * nch + c) * 2] for c in range(nch))  # take hi byte
    else:  # 1, 2, 4 bits, MSB-first
        per = 8 // depth
        mask = (1 << depth) - 1
        for x in range(w):
            vals = []
            for c in range(nch):
                idx = x * nch + c
                byte = line[idx // per]
                shift = 8 - depth * (idx % per + 1)
                vals.append((byte >> shift) & mask)
            yield tuple(vals)


def decode_png(path):
    """-> (w, h, pixels) where pixels is a flat list of (r,g,b,a) 8-bit tuples."""
    blob = open(path, "rb").read()
    w = h = depth = ctype = interlace = None
    plte, trns, idat = None, None, bytearray()
    for typ, data in png_chunks(blob):
        if typ == b"IHDR":
            w = int.from_bytes(data[0:4], "big")
            h = int.from_bytes(data[4:8], "big")
            depth, ctype, interlace = data[8], data[9], data[12]
        elif typ == b"PLTE":
            plte = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
        elif typ == b"tRNS":
            trns = data
        elif typ == b"IDAT":
            idat += data
    if interlace:
        raise NotImplementedError("interlaced PNG not supported: " + path)

    nch = CHANNELS[ctype]
    stride = (w * nch * depth + 7) // 8
    bpp = max(1, (nch * depth + 7) // 8)
    lines = unfilter(zlib.decompress(bytes(idat)), h, stride, bpp)

    mx = (1 << depth) - 1
    px = []
    for y in range(h):
        line = lines[y * stride:(y + 1) * stride]
        for s in samples(line, w, nch, depth):
            if ctype == 3:                       # palette
                r, g, b = plte[s[0]]
                a = trns[s[0]] if trns and s[0] < len(trns) else 255
            elif ctype == 0:                     # grey
                v = s[0] * 255 // mx
                r = g = b = v
                a = 255
            elif ctype == 4:                     # grey + alpha
                v = s[0] * 255 // mx
                r = g = b = v
                a = s[1] * 255 // mx
            elif ctype == 2:                     # rgb
                r, g, b = (c * 255 // mx for c in s)
                a = 255
            else:                                # rgba
                r, g, b = (c * 255 // mx for c in s[:3])
                a = s[3] * 255 // mx
            px.append((r, g, b, a))
    return w, h, px


# ----------------------------------------------------------------------- packing
def c_ident(name):
    """Turn an asset name into a C identifier: Iosevka-Regular-30 -> iosevka_regular_30."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def rgb565_words(data):
    """Raw bytes (hi,lo per pixel) -> list of uint16 values."""
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]


def slice_glyph(words, img_w, x0, cell_w, cell_h):
    """Copy one cell_w*cell_h glyph cell out of the atlas at column x0."""
    out = []
    for y in range(cell_h):
        row = y * img_w + x0
        out.extend(words[row:row + cell_w])
    return out


MARKER = (255, 0, 255)   # magenta: cell-origin markers in the font atlases


def font_metrics(w, h, px):
    """Derive the glyph grid from the magenta cell-origin markers in row 0.

    The atlas is self-describing: one marker per cell at its top-left corner, so the
    marker spacing IS the cell advance and the marker count IS the glyph count.
    Returns None when the image carries no markers (i.e. it is not a font atlas).
    """
    xs = sorted(x for x in range(w) if px[x][:3] == MARKER)   # row 0 only
    if len(xs) < 2:
        return None
    deltas = {xs[i + 1] - xs[i] for i in range(len(xs) - 1)}
    if len(deltas) != 1:
        raise ValueError("non-uniform glyph advance: %s" % sorted(deltas))
    return {"cell_w": deltas.pop(), "cell_h": h, "count": len(xs), "first_char": 0x20}


def to_rgb565(w, h, px):
    """16bpp big-endian (hi byte first), alpha composited over black.

    MARKER pixels are metadata (glyph cell origins), not art, so they resolve to the
    background colour rather than magenta.
    """
    out = bytearray()
    for p in px:
        r, g, b, a = p
        if p[:3] == MARKER:
            r = g = b = 0
        elif a != 255:
            r, g, b = r * a // 255, g * a // 255, b * a // 255
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out += bytes((v >> 8, v & 0xFF))
    return bytes(out), w * 2


# --------------------------------------------------------------------------- flash
# --flash packs every asset into one image to be written at FLASH_ASSET_BASE:
#
#   +0x0000  index sector (4K): magic, version, count, then 16-byte entries
#   +0x1000  assets, each page-aligned so a single asset can be rewritten
#            without disturbing its neighbours
#
# Entry addresses are stored RELATIVE to the region base, so the whole blob can
# be relocated by writing it somewhere else and telling the firmware where.
#
# The pixel bytes are BYTE-SWAPPED here to lo-byte-first (raw/ holds hi-byte-first;
# see the module docstring for why the DMA path needs lo-first).
FLASH_MAGIC   = b"AKAS"
FLASH_VERSION = 1
FLASH_INDEX   = 0x1000     # index sector size; assets start here
FLASH_PAGE    = 256
FMT_IMAGE     = 0
FMT_FONT      = 1


def emit_flash(entries, raw_dir, out_bin, out_h):
    items, off = [], FLASH_INDEX
    for i, e in enumerate(sorted(entries, key=lambda x: x["name"])):
        data = open(os.path.join(raw_dir, e["name"] + ".raw"), "rb").read()
        f = e.get("font")
        if f:
            # A glyph cell inside the atlas is STRIDED -- its rows are cell_w wide
            # but sit img_w apart. The DMA streams consecutive bytes and cannot
            # skip, so an atlas is undrawable by it. Repack into per-glyph
            # contiguous tiles: glyph n is then one flat cell_w*cell_h blit at
            # off + n*cell_w*cell_h*2.
            words = rgb565_words(data)
            tiles = []
            for n in range(f["count"]):
                tiles.extend(slice_glyph(words, e["width"], n * f["cell_w"],
                                         f["cell_w"], f["cell_h"]))
            data = b"".join(struct.pack(">H", wv) for wv in tiles)
        data = bytes(b for pair in zip(data[1::2], data[0::2]) for b in pair)  # -> lo-byte-first
        items.append({
            "id": i, "name": e["name"], "off": off, "data": data,
            "w": f["cell_w"] if f else e["width"],
            "h": f["cell_h"] if f else e["height"],
            "fmt": FMT_FONT if f else FMT_IMAGE,
            "cell_w": f["cell_w"] if f else 0, "cell_h": f["cell_h"] if f else 0,
            "first": f["first_char"] if f else 0, "count": f["count"] if f else 1,
        })
        off += (len(data) + FLASH_PAGE - 1) // FLASH_PAGE * FLASH_PAGE

    idx = bytearray(FLASH_INDEX)
    idx[0:4] = FLASH_MAGIC
    idx[4] = FLASH_VERSION
    idx[5] = len(items)
    for n, it in enumerate(items):
        p = 8 + n * 16
        idx[p:p+2]   = struct.pack("<H", it["id"])
        idx[p+2:p+5] = struct.pack("<I", it["off"])[:3]      # u24, region-relative
        idx[p+5]     = it["fmt"]
        idx[p+6:p+8]   = struct.pack("<H", it["w"])
        idx[p+8:p+10]  = struct.pack("<H", it["h"])
        idx[p+10] = it["cell_w"]; idx[p+11] = it["cell_h"]
        idx[p+12] = it["first"];  idx[p+13] = min(it["count"], 255)

    blob = bytearray(idx)
    for it in items:
        blob.extend(bytes(it["off"] - len(blob)))            # pad to the entry offset
        blob.extend(it["data"])
    blob.extend(bytes((-len(blob)) % FLASH_PAGE))

    with open(out_bin, "wb") as fh:
        fh.write(blob)
    with open(out_h, "w") as fh:
        fh.write("// AUTO-GENERATED by mkraw.py --flash -- DO NOT EDIT.\n"
                 "// Asset ids for the flash-resident set; the firmware reads the index\n"
                 "// sector at FLASH_ASSET_BASE and looks entries up by these ids.\n\n"
                 "#pragma once\n\n"
                 "#define FLASH_ASSET_MAGIC 0x%08X\n" % int.from_bytes(FLASH_MAGIC, "little") +
                 "#define FLASH_ASSET_VERSION %d\n"
                 "#define FLASH_ASSET_COUNT %d\n\n" % (FLASH_VERSION, len(items)) +
                 "enum {\n")
        for it in items:
            fh.write("    ASSET_%-28s = %d,   // %dx%d%s\n" % (
                c_ident(it["name"]).upper(), it["id"], it["w"], it["h"],
                ", font %dx%d" % (it["cell_w"], it["cell_h"]) if it["fmt"] == FMT_FONT else ""))
        fh.write("};\n")

    print("\n--flash: %d assets, index %d B + data -> %d B total" % (len(items), FLASH_INDEX, len(blob)))
    for it in items:
        print("   id %2d  +0x%06X  %-28s %6d B%s" % (
            it["id"], it["off"], it["name"], len(it["data"]),
            "  (font)" if it["fmt"] == FMT_FONT else ""))
    print("upload with:  ak820ctl flash write 0x0CE0000 %s" % os.path.basename(out_bin))


# -------------------------------------------------------------------------- main
def main():
    flash = "--flash" in sys.argv
    write = flash or "--write" in sys.argv
    pngs = sorted(f for f in os.listdir(HERE) if f.lower().endswith(".png"))
    if not pngs:
        print("no PNGs found in", HERE)
        return 1
    if write:
        os.makedirs(OUTDIR, exist_ok=True)

    manifest = []
    print("%-30s %5s %5s  %-7s %6s %8s  %s" % ("source", "w", "h", "format", "colors", "bytes", "notes"))
    print("-" * 88)
    for f in pngs:
        w, h, px = decode_png(os.path.join(HERE, f))
        colors = sorted({p for p in px})
        # A magenta marker row means this is a font atlas. Everything (icons, splash,
        # fonts) is emitted as rgb565; the icons are NOT all monochrome (bluetooth is
        # blue, others carry a grey), so they keep their real colours.
        metrics = font_metrics(w, h, px)
        data, stride, fmt, depth = *to_rgb565(w, h, px), "rgb565", 16
        note = ""
        if metrics:
            note = "font atlas: %d glyphs @ %dx%d, first=0x%02X (fg/bg baked)" % (
                metrics["count"], metrics["cell_w"], metrics["cell_h"], metrics["first_char"])
        name = os.path.splitext(f)[0]
        print("%-30s %5d %5d  %-7s %6d %8d  %s" % (f, w, h, fmt, len(colors), len(data), note))
        entry = {
            "name": name,
            "source": f,
            "raw": "raw/%s.raw" % name,
            "width": w,
            "height": h,
            "format": fmt,          # rgb565
            "depth": depth,         # bits per pixel
            "stride": stride,       # bytes per row
            "bytes": len(data),
            "colors": len(colors),
        }
        if metrics:
            entry["font"] = metrics          # cell_w, cell_h, count, first_char
        else:
            entry["palette"] = ["#%02X%02X%02X%02X" % c for c in colors] if len(colors) <= 8 else None
        manifest.append(entry)
        if write:
            with open(os.path.join(OUTDIR, name + ".raw"), "wb") as fh:
                fh.write(data)

    total = sum(e["bytes"] for e in manifest)
    print("-" * 72)
    print("%-30s %25s %8d" % ("TOTAL", "", total))
    if write:
        with open(os.path.join(OUTDIR, "manifest.json"), "w") as fh:
            json.dump({"assets": manifest}, fh, indent=2)
            fh.write("\n")
        print("\nwrote %d raw files + manifest.json to %s" % (len(manifest), OUTDIR))
    else:
        print("\n(inspect only -- rerun with --write to emit raw/ + manifest.json)")

    if flash:
        emit_flash(manifest, OUTDIR,
                   os.path.join(HERE, "flash_assets.bin"),
                   os.path.join(HERE, "flash_assets.h"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
