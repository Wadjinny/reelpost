"""Tiny MP4-like files for duration and faststart checks."""

import struct
from pathlib import Path


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def mvhd(timescale: int, duration: int) -> bytes:
    # version/flags, creation, modification, timescale, duration
    payload = struct.pack(">III", 0, 0, 0) + struct.pack(">II", timescale, duration)
    payload += b"\x00" * 80
    return box(b"mvhd", payload)


def mvhd_v1(timescale: int, duration: int) -> bytes:
    payload = bytearray(32)
    payload[0] = 1
    struct.pack_into(">I", payload, 20, timescale)
    struct.pack_into(">Q", payload, 24, duration)
    return box(b"mvhd", bytes(payload))


def tkhd(width: int, height: int) -> bytes:
    payload = bytearray(84)
    struct.pack_into(">II", payload, 76, int(width * 65536), int(height * 65536))
    return box(b"tkhd", bytes(payload))


def movie(timescale: int, duration: int, width: int = 1080, height: int = 1920, *, faststart: bool = True, version: int = 0) -> bytes:
    header = mvhd_v1(timescale, duration) if version == 1 else mvhd(timescale, duration)
    moov = box(b"moov", header + box(b"trak", tkhd(width, height)))
    mdat = box(b"mdat", b"\x00" * 16)
    if faststart:
        return moov + mdat
    return mdat + moov


def write_movie(path: Path, **kwargs) -> Path:
    path.write_bytes(movie(**kwargs))
    return path
