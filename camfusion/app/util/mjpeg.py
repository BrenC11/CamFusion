from __future__ import annotations

from typing import Generator

BOUNDARY = b"frame"


def multipart_chunk(jpeg: bytes, boundary: bytes = BOUNDARY) -> bytes:
    header = (
        b"--"
        + boundary
        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpeg)).encode("ascii")
        + b"\r\n\r\n"
    )
    return header + jpeg + b"\r\n"


class MJPEGByteParser:
    """Split concatenated JPEG-bytes stream into complete JPEG frames."""

    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Generator[bytes, None, None]:
        self._buffer.extend(data)

        while True:
            start = self._buffer.find(self.SOI)
            if start < 0:
                if len(self._buffer) > 1024 * 1024:
                    del self._buffer[:-2]
                return

            end = self._buffer.find(self.EOI, start + 2)
            if end < 0:
                if start > 0:
                    del self._buffer[:start]
                return

            frame_end = end + 2
            frame = bytes(self._buffer[start:frame_end])
            del self._buffer[:frame_end]
            yield frame
