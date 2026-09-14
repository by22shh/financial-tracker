"""Настоящие маленькие изображения для проверок (SEC-07, G-26).

Байты вложения проверяются до платного вызова, поэтому фикстуры должны быть
реальными файлами формата, а не произвольной строкой.
"""

from __future__ import annotations

import struct
import zlib


def png_bytes(width: int = 8, height: int = 8, *, marker: bytes = b"") -> bytes:
    """Корректный PNG заданного размера.

    ``marker`` попадает в текстовый блок: разные вложения дают разные байты и
    разный отпечаток, оставаясь настоящими изображениями.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    parts = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", header), chunk(b"IDAT", zlib.compress(raw))]
    if marker:
        parts.append(chunk(b"tEXt", b"fixture\x00" + marker))
    parts.append(chunk(b"IEND", b""))
    return b"".join(parts)
