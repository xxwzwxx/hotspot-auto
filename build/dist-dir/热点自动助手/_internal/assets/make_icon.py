"""生成 desktop/assets/app.ico 与 preview.png（纯标准库，不依赖 Pillow）。

图形：圆角方块（蓝紫渐变）+ 白色 WiFi 波纹，用作程序图标和托盘图标。
直接运行：python make_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent
MASTER = 256
SIZES = (256, 128, 64, 48, 32, 24, 16)

BG_FROM = (37, 99, 235)  # #2563EB
BG_TO = (124, 58, 237)  # #7C3AED
GLYPH = (255, 255, 255)
GLYPH_ALPHA = 245

CORNER_RADIUS = 0.20
CENTER = (0.5, 0.70)
RING_RADII = (0.145, 0.275, 0.405)
RING_HALF_WIDTH = 0.037
DOT_RADIUS = 0.058
ARC_HALF_ANGLE_COS = math.cos(math.radians(56))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _glyph_alpha(nx: float, ny: float) -> float:
    gx, gy = nx - CENTER[0], ny - CENTER[1]
    dist = math.hypot(gx, gy)
    if dist <= DOT_RADIUS:
        return 1.0
    up = -gy  # 屏幕坐标 y 向下，向上为负
    if up < ARC_HALF_ANGLE_COS * dist:
        return 0.0
    for radius in RING_RADII:
        if abs(dist - radius) <= RING_HALF_WIDTH:
            return 1.0
    return 0.0


def _background_alpha(nx: float, ny: float) -> float:
    rad = CORNER_RADIUS
    dx = max(abs(nx - 0.5) - (0.5 - rad), 0.0)
    dy = max(abs(ny - 0.5) - (0.5 - rad), 0.0)
    return 1.0 if dx * dx + dy * dy <= rad * rad else 0.0


def render_master(size: int = MASTER, supersample: int = 3) -> bytearray:
    pixels = bytearray(size * size * 4)
    step = 1.0 / (size * supersample)
    samples = supersample * supersample
    for y in range(size):
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(supersample):
                ny = (y * supersample + sy + 0.5) * step
                for sx in range(supersample):
                    nx = (x * supersample + sx + 0.5) * step
                    bg = _background_alpha(nx, ny)
                    if bg <= 0.0:
                        continue
                    t = (nx + ny) * 0.5
                    r = _lerp(BG_FROM[0], BG_TO[0], t)
                    g = _lerp(BG_FROM[1], BG_TO[1], t)
                    b = _lerp(BG_FROM[2], BG_TO[2], t)
                    a = 1.0
                    mask = _glyph_alpha(nx, ny)
                    if mask > 0.0:
                        ga = GLYPH_ALPHA / 255.0
                        r = _lerp(r, GLYPH[0], ga * mask)
                        g = _lerp(g, GLYPH[1], ga * mask)
                        b = _lerp(b, GLYPH[2], ga * mask)
                    acc_r += r * a
                    acc_g += g * a
                    acc_b += b * a
                    acc_a += a
            index = (y * size + x) * 4
            if acc_a <= 0.0:
                pixels[index : index + 4] = b"\x00\x00\x00\x00"
            else:
                pixels[index + 0] = int(round(min(255.0, acc_r / acc_a)))
                pixels[index + 1] = int(round(min(255.0, acc_g / acc_a)))
                pixels[index + 2] = int(round(min(255.0, acc_b / acc_a)))
                pixels[index + 3] = int(round(min(255.0, acc_a / samples * 255.0)))
    return pixels


def resize_box(source: bytearray, src_size: int, dst_size: int) -> bytearray:
    """整数倍区域平均缩放，带 alpha 加权，避免边缘发黑。"""
    factor = src_size // dst_size
    out = bytearray(dst_size * dst_size * 4)
    for y in range(dst_size):
        for x in range(dst_size):
            r = g = b = a = 0.0
            for sy in range(factor):
                row = (y * factor + sy) * src_size
                for sx in range(factor):
                    index = (row + x * factor + sx) * 4
                    alpha = source[index + 3] / 255.0
                    r += source[index + 0] * alpha
                    g += source[index + 1] * alpha
                    b += source[index + 2] * alpha
                    a += alpha
            total = factor * factor
            index = (y * dst_size + x) * 4
            if a <= 0.0:
                continue
            out[index + 0] = int(round(r / a))
            out[index + 1] = int(round(g / a))
            out[index + 2] = int(round(b / a))
            out[index + 3] = int(round(a / total * 255.0))
    return out


def png_bytes(size: int, rgba: bytearray) -> bytes:
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    blobs = b""
    for size, png in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    path.write_bytes(header + entries + blobs)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = render_master(MASTER, supersample=3)
    cache: dict[int, bytearray] = {MASTER: master}
    for size in SIZES:
        if size == MASTER:
            continue
        cache[size] = resize_box(master, MASTER, size)

    images = [(size, png_bytes(size, cache[size])) for size in SIZES]
    write_ico(ASSETS / "app.ico", images)
    (ASSETS / "preview.png").write_bytes(png_bytes(MASTER, master))
    print(f"app.ico: {sum(len(p) for _, p in images)} bytes, {len(images)} sizes")
    print(f"preview.png written")


if __name__ == "__main__":
    main()
