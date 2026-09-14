"""Проверка фактических байтов изображения до платного вызова (SEC-07, NFR-11).

Заявленные Telegram MIME и размеры — метаданные отправителя: файл с типом
`image/jpeg` может содержать что угодно. Поэтому перед обращением к модели
проверяются сигнатура формата, целостность заголовка и настоящее разрешение
(LIM-09, G-26). Разбор идёт по заголовку без декодирования всей картинки:
несуществующий или повреждённый заголовок отклоняется.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBP_PREFIX = b"RIFF"
WEBP_TAG = b"WEBP"

SUPPORTED_MEDIA_TYPES = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

# Маркеры начала кадра JPEG, содержащие размеры. DHT/DAC/RST и маркеры без
# длины исключены намеренно.
_JPEG_SOF = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class InvalidImage(ValueError):
    """Файл не является поддерживаемым изображением или повреждён."""


@dataclass(frozen=True, slots=True)
class ImageFacts:
    """Проверенные свойства файла, а не заявленные отправителем."""

    media_type: str
    width: int
    height: int
    size_bytes: int
    # Заявленный отправителем тип: метаданные, решение принимается по файлу.
    declared_media_type: str | None = None

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def declared_type_matches(self) -> bool:
        if not self.declared_media_type:
            return True
        return self.declared_media_type.lower() == self.media_type


def _jpeg_size(data: bytes) -> tuple[int, int]:
    offset = 2
    total = len(data)
    while offset + 3 < total:
        if data[offset] != 0xFF:
            raise InvalidImage("Повреждённая структура JPEG")
        marker = data[offset + 1]
        offset += 2
        while marker == 0xFF and offset < total:
            marker = data[offset]
            offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 1 >= total:
            break
        (length,) = struct.unpack(">H", data[offset : offset + 2])
        if length < 2 or offset + length > total:
            raise InvalidImage("Повреждённый сегмент JPEG")
        if marker in _JPEG_SOF:
            if length < 7:
                raise InvalidImage("Повреждённый заголовок кадра JPEG")
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return int(width), int(height)
        offset += length
    raise InvalidImage("В JPEG не найден заголовок кадра")


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise InvalidImage("Повреждённый заголовок PNG")
    ihdr_length = struct.unpack(">I", data[8:12])[0]
    if ihdr_length != 13:
        raise InvalidImage("Повреждённый заголовок PNG")
    ihdr_end = 8 + 12 + ihdr_length
    expected_crc = struct.unpack(">I", data[ihdr_end - 4 : ihdr_end])[0]
    actual_crc = zlib.crc32(data[12 : ihdr_end - 4]) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise InvalidImage("Повреждённая контрольная сумма PNG")
    width, height = struct.unpack(">II", data[16:24])
    offset = ihdr_end
    seen_idat = False
    seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(data):
            raise InvalidImage("Повреждённая структура PNG")
        expected_crc = struct.unpack(">I", data[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + data[data_start:data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise InvalidImage("Повреждённая контрольная сумма PNG")
        if chunk_type == b"IDAT":
            seen_idat = True
        elif chunk_type == b"IEND":
            seen_iend = True
            if crc_end != len(data):
                raise InvalidImage("Лишние данные после PNG")
            break
        offset = crc_end
    if not seen_idat or not seen_iend:
        raise InvalidImage("PNG не содержит полного изображения")
    return int(width), int(height)


def _webp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        raise InvalidImage("Повреждённый заголовок WebP")
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise InvalidImage("Повреждённый кадр WebP")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise InvalidImage("Неизвестный формат WebP")


def inspect_image(
    data: bytes,
    *,
    max_bytes: int,
    max_pixels: int,
    max_side: int,
    declared_media_type: str | None = None,
) -> ImageFacts:
    """Проверить файл и вернуть его настоящие свойства.

    Заявленный тип сохраняется справочно: решение и media type для провайдера
    берутся из содержимого файла, а не из метаданных отправителя.
    """
    if not data:
        raise InvalidImage("Пустой файл")
    if len(data) > max_bytes:
        raise InvalidImage("Файл больше допустимого размера")

    if data.startswith(JPEG_MAGIC):
        kind = "jpeg"
        width, height = _jpeg_size(data)
    elif data.startswith(PNG_MAGIC):
        kind = "png"
        width, height = _png_size(data)
    elif data.startswith(WEBP_PREFIX) and data[8:12] == WEBP_TAG:
        kind = "webp"
        width, height = _webp_size(data)
    else:
        raise InvalidImage("Файл не является изображением JPEG, PNG или WebP")

    if width <= 0 or height <= 0:
        raise InvalidImage("Недопустимые размеры изображения")
    if width * height > max_pixels:
        raise InvalidImage("Изображение слишком большое для безопасной обработки")
    if max(width, height) > max_side:
        raise InvalidImage("Слишком большая сторона изображения")

    return ImageFacts(
        media_type=SUPPORTED_MEDIA_TYPES[kind],
        width=width,
        height=height,
        size_bytes=len(data),
        declared_media_type=declared_media_type,
    )
