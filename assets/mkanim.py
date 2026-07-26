#!/usr/bin/env python3
"""
AK820 Pro animation packer: GIF -> flash animation blob.

    python3 mkanim.py input.gif [-o out.bin] [--fit contain|cover] [--max N]

Then upload it to the stock user-animation slot:

    ak820ctl flash write 0x540000 out.bin --unlock

Output layout (the stock format, reverse-engineered from flash):
    0x000        frame count, one byte
    0x001..      one duration byte per frame
    ...          padding to 0x100
    0x100+       frames, 128*128 RGB565 lo-byte-first, 0x8000 each

The duration bytes are written because the stock format has them, but NOTHING
READS THEM -- not our firmware and not V1.13's (disassembly shows its player
only reads the count). Playback is paced by the firmware at one frame per
100 ms, so a GIF authored at a different rate will not keep its original speed.

Byte order is LO-BYTE-FIRST, unlike the hi-byte-first tiles mkraw.py emits for
the CPU path: the DMA runs the SPI in 16-bit mode, which swaps each pair. See
docs/LCD_FLASH_LAYER.md.

No third-party deps -- GIF is decoded here (LZW, interlace, frame disposal),
since Pillow/ffmpeg/ImageMagick are not available in this tree.
"""

import os
import sys

FRAME_W = FRAME_H = 128
FRAME_BYTES = FRAME_W * FRAME_H * 2
HDR = 0x100
# Matches the firmware's derived ceiling: the slot must not reach the asset
# region at 0x0CE0000, and the count is one byte.
MAX_FRAMES = min((0x0CE0000 - 0x540000 - HDR) // 0x8000, 255)


# ----------------------------------------------------------------- GIF decode
def lzw(data, min_code_size):
    """GIF's variable-width LZW. Returns a list of palette indices."""
    clear, end = 1 << min_code_size, (1 << min_code_size) + 1
    dic = [bytes([i]) for i in range(clear)] + [b"", b""]
    size, prev, out = min_code_size + 1, None, bytearray()
    acc = nbits = 0
    for byte in data:
        acc |= byte << nbits
        nbits += 8
        while nbits >= size:
            code, acc, nbits = acc & ((1 << size) - 1), acc >> size, nbits - size
            if code == clear:
                dic = dic[:clear + 2]
                size, prev = min_code_size + 1, None
                continue
            if code == end:
                return out
            if code < len(dic):
                entry = dic[code]
            elif prev is not None:
                entry = prev + prev[:1]      # the classic KwKwK case
            else:
                return out                   # malformed
            out += entry
            if prev is not None:
                dic.append(prev + entry[:1])
                # Widen once the next code would not fit, up to 12 bits.
                if len(dic) >= (1 << size) and size < 12:
                    size += 1
            prev = entry
    return out


def blocks(buf, i):
    """Read a GIF sub-block chain; returns (data, next_index)."""
    out = bytearray()
    while buf[i]:
        n = buf[i]
        out += buf[i + 1:i + 1 + n]
        i += 1 + n
    return bytes(out), i + 1


def deinterlace(rows, h):
    out = [None] * h
    src = 0
    for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)):
        for y in range(start, h, step):
            out[y] = rows[src]
            src += 1
    return out


def decode_gif(path):
    """-> (canvas_w, canvas_h, [ (rgb_bytes, delay_cs), ... ])

    Frames are composited: GIF stores each frame as a patch over the previous
    canvas, with a disposal method saying what to do afterwards. Decoding frames
    independently gives torn output on anything but the simplest GIFs.
    """
    b = open(path, "rb").read()
    if b[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF: " + path)
    w = b[6] | (b[7] << 8)
    h = b[8] | (b[9] << 8)
    flags, bg = b[10], b[11]
    i = 13
    gct = None
    if flags & 0x80:
        n = 2 << (flags & 7)
        gct = [tuple(b[i + k * 3:i + k * 3 + 3]) for k in range(n)]
        i += n * 3

    canvas = [(0, 0, 0)] * (w * h)          # composited RGB
    frames = []
    delay, transparent, disposal = 10, None, 0

    while i < len(b):
        blk = b[i]
        if blk == 0x3B:                      # trailer
            break
        if blk == 0x21:                      # extension
            label = b[i + 1]
            if label == 0xF9:                # graphic control
                size = b[i + 2]
                pk = b[i + 3]
                delay = b[i + 4] | (b[i + 5] << 8)
                transparent = b[i + 6] if pk & 1 else None
                disposal = (pk >> 2) & 7
                i += 3 + size
                if i < len(b) and b[i] == 0:
                    i += 1                   # block terminator (tolerate its absence)
            else:
                _, i = blocks(b, i + 2)
            continue
        if blk != 0x2C:                      # unexpected -- stop rather than guess
            break

        # image descriptor
        x0 = b[i + 1] | (b[i + 2] << 8)
        y0 = b[i + 3] | (b[i + 4] << 8)
        iw = b[i + 5] | (b[i + 6] << 8)
        ih = b[i + 7] | (b[i + 8] << 8)
        f  = b[i + 9]
        i += 10
        pal = gct
        if f & 0x80:                          # local colour table
            n = 2 << (f & 7)
            pal = [tuple(b[i + k * 3:i + k * 3 + 3]) for k in range(n)]
            i += n * 3
        mcs = b[i]
        data, i = blocks(b, i + 1)
        idx = lzw(data, mcs)

        rows = [idx[r * iw:(r + 1) * iw] for r in range(ih)]
        if f & 0x40:
            rows = deinterlace(rows, ih)

        saved = list(canvas) if disposal == 3 else None
        for r, row in enumerate(rows):
            y = y0 + r
            if y >= h:
                break
            for c, pi in enumerate(row):
                x = x0 + c
                if x >= w or pi == transparent or pi >= len(pal):
                    continue
                canvas[y * w + x] = pal[pi]

        out = bytearray()
        for px in canvas:
            out += bytes(px)
        frames.append((bytes(out), delay))

        # Disposal: 2 = restore to background, 3 = restore to previous.
        if disposal == 2:
            fill = gct[bg] if gct and bg < len(gct) else (0, 0, 0)
            for y in range(y0, min(y0 + ih, h)):
                for x in range(x0, min(x0 + iw, w)):
                    canvas[y * w + x] = fill
        elif disposal == 3 and saved:
            canvas = saved

    return w, h, frames


# ------------------------------------------------------------------- resample
def to_frame(rgb, w, h, mode):
    """Nearest-neighbour scale + centre crop/pad to 128x128 -> RGB565 lo-first."""
    if mode == "cover":
        s = max(FRAME_W / w, FRAME_H / h)
    else:                                    # contain: whole image, letterboxed
        s = min(FRAME_W / w, FRAME_H / h)
    dw, dh = max(1, int(w * s + 0.5)), max(1, int(h * s + 0.5))
    ox, oy = (FRAME_W - dw) // 2, (FRAME_H - dh) // 2

    out = bytearray(FRAME_BYTES)
    for y in range(FRAME_H):
        sy = int((y - oy) * h / dh)
        if not (0 <= y - oy < dh) or not (0 <= sy < h):
            continue
        for x in range(FRAME_W):
            sx = int((x - ox) * w / dw)
            if not (0 <= x - ox < dw) or not (0 <= sx < w):
                continue
            p = (sy * w + sx) * 3
            r, g, bl = rgb[p], rgb[p + 1], rgb[p + 2]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (bl >> 3)
            o = (y * FRAME_W + x) * 2
            out[o] = v & 0xFF                # lo byte first -- the DMA swaps
            out[o + 1] = v >> 8
    return bytes(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__.strip())
        return 1
    src = args[0]
    out = args[1] if len(args) > 1 else os.path.splitext(src)[0] + ".bin"
    if "-o" in sys.argv:
        out = sys.argv[sys.argv.index("-o") + 1]
    mode = "cover"
    if "--fit" in sys.argv:
        mode = sys.argv[sys.argv.index("--fit") + 1]
    cap = MAX_FRAMES
    if "--max" in sys.argv:
        cap = min(cap, int(sys.argv[sys.argv.index("--max") + 1]))

    w, h, frames = decode_gif(src)
    print("%s: %dx%d, %d frames" % (os.path.basename(src), w, h, len(frames)))
    if not frames:
        print("no frames decoded")
        return 1

    dropped = 0
    if len(frames) > cap:
        # Drop evenly across the animation rather than truncating the tail, so a
        # long GIF still reads as the whole loop.
        keep = [frames[int(i * len(frames) / cap)] for i in range(cap)]
        dropped, frames = len(frames) - cap, keep
        print("  too long for the slot: sampled down to %d frames (dropped %d)" % (cap, dropped))

    blob = bytearray(HDR)
    blob[0] = len(frames)
    for n, (_, delay) in enumerate(frames):
        # Written for format fidelity only; nothing reads these.
        blob[1 + n] = min(255, max(1, delay))
    for rgb, _ in frames:
        blob += to_frame(rgb, w, h, mode)

    with open(out, "wb") as fh:
        fh.write(blob)

    total_cs = sum(d for _, d in frames)
    print("  fit=%s  ->  %s  (%d bytes, %d frames)" % (mode, out, len(blob), len(frames)))
    print("  source timing ~%.1fs total; playback is a fixed 100 ms/frame = %.1fs"
          % (total_cs / 100.0, len(frames) * 0.1))
    print("\nupload with:\n  ak820ctl flash write 0x540000 %s --unlock" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
