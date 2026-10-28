"""Local video and cover checks before anything is uploaded."""

from __future__ import annotations

import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

from reelpost.constants import (
    FB_COVER_MAX_BYTES,
    FB_MAX_SECONDS,
    FB_MIN_LONG_SIDE,
    FB_MIN_SECONDS,
    FB_MIN_SHORT_SIDE,
    IG_COVER_MAX_BYTES,
    IG_MAX_BYTES,
    IG_MAX_SECONDS,
    IG_MIN_SECONDS,
    YT_COVER_MAX_BYTES,
    YT_MAX_SECONDS,
    YT_MIN_SECONDS,
)
from reelpost.errors import ReelpostError

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}


@dataclass
class VideoInfo:
    path: Path
    size_bytes: int
    duration_seconds: float | None
    width: int | None
    height: int | None
    faststart: bool | None


@dataclass
class CoverAsset:
    path: Path
    width: int
    height: int
    temporary_dir: Path | None
    reencoded: bool

    def close(self) -> None:
        if self.temporary_dir is not None:
            shutil.rmtree(self.temporary_dir, ignore_errors=True)
            self.temporary_dir = None


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def format_seconds(value: float) -> str:
    if value >= 60:
        total = int(round(value))
        minutes, seconds = divmod(total, 60)
        return f"{minutes}m {seconds}s"
    return f"{value:.1f}s"


def require_video(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ReelpostError(f"Video not found: {path}")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ReelpostError(f"Video must be an .mp4 or .mov file (got {path.name}).")
    if path.stat().st_size <= 0:
        raise ReelpostError(f"Video is empty: {path}")
    return path


def inspect_video(path: Path) -> VideoInfo:
    path = require_video(path)
    size = path.stat().st_size
    duration = None
    width = None
    height = None
    faststart = None
    with path.open("rb") as handle:
        boxes = list(_iter_boxes(handle, 0, size))
        moov_at = _first(boxes, b"moov")
        mdat_at = _first(boxes, b"mdat")
        if moov_at is not None and mdat_at is not None:
            faststart = moov_at < mdat_at
        for typ, start, end in boxes:
            if typ != b"moov":
                continue
            duration = _find_duration(handle, start, end)
            width, height = _find_dimensions(handle, start, end)
            break
    return VideoInfo(path, size, duration, width, height, faststart)


def limits_for(info: VideoInfo, targets: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if "instagram" in targets:
        if info.size_bytes > IG_MAX_BYTES:
            errors.append(
                f"Instagram Reels must be 300 MB or smaller (this file is {format_bytes(info.size_bytes)})."
            )
        if info.duration_seconds is None:
            warnings.append("Could not read the video duration. Instagram allows 3 seconds to 15 minutes.")
        elif not _within(info.duration_seconds, IG_MIN_SECONDS, IG_MAX_SECONDS):
            errors.append(
                "Instagram Reels must be between 3 seconds and 15 minutes "
                f"(this video is {format_seconds(info.duration_seconds)})."
            )
        if info.faststart is False:
            errors.append(
                "Instagram requires the moov atom at the front of the file. Remux it with: "
                f"ffmpeg -i {info.path} -c copy -movflags +faststart OUTPUT.mp4"
            )
        elif info.faststart is None:
            warnings.append("Could not tell whether the moov atom is at the front of the file.")
    if "facebook" in targets:
        if info.duration_seconds is None:
            warnings.append("Could not read the video duration. Facebook Reels must be 3 to 90 seconds.")
        elif not _within(info.duration_seconds, FB_MIN_SECONDS, FB_MAX_SECONDS):
            errors.append(
                "Facebook Reels must be between 3 and 90 seconds "
                f"(this video is {format_seconds(info.duration_seconds)})."
            )
        if info.width and info.height:
            short = min(info.width, info.height)
            long = max(info.width, info.height)
            ratio = info.width / info.height
            if short < FB_MIN_SHORT_SIDE or long < FB_MIN_LONG_SIDE:
                errors.append(
                    f"Facebook Reels need at least 540×960 (this video is {info.width}×{info.height})."
                )
            if ratio < (9 / 16) - 0.01 or ratio > (16 / 9) + 0.01:
                errors.append(
                    "Facebook Reel aspect ratio must be between 9:16 and 16:9 "
                    f"(this video is {info.width}×{info.height})."
                )
        else:
            warnings.append("Could not read video dimensions. Facebook recommends 1080×1920.")
    if "youtube" in targets:
        if info.duration_seconds is None:
            warnings.append("Could not read the video duration. YouTube Shorts must be 3 minutes or shorter.")
        elif not _within(info.duration_seconds, YT_MIN_SECONDS, YT_MAX_SECONDS):
            errors.append(
                "YouTube Shorts must be between 1 second and 3 minutes "
                f"(this video is {format_seconds(info.duration_seconds)})."
            )
        if info.width and info.height:
            if info.width > info.height:
                errors.append(
                    "YouTube Shorts must be vertical or square "
                    f"(this video is {info.width}×{info.height})."
                )
        else:
            warnings.append("Could not read video dimensions. YouTube Shorts should be vertical, ideally 1080×1920.")
    return errors, warnings


def cover_limit(targets: list[str]) -> int:
    """The strictest cover size among the selected platforms."""
    limits = []
    if "instagram" in targets:
        limits.append(IG_COVER_MAX_BYTES)
    if "facebook" in targets:
        limits.append(FB_COVER_MAX_BYTES)
    if "youtube" in targets:
        limits.append(YT_COVER_MAX_BYTES)
    return min(limits) if limits else FB_COVER_MAX_BYTES


def cover_warning(width: int, height: int) -> str | None:
    if width <= 0 or height <= 0:
        return None
    if abs((width / height) - (9 / 16)) > 0.02:
        return (
            f"Cover is {width}×{height}. A 9:16 image avoids cropping. "
            "Instagram uses the center 9:16, and the profile feed uses the center square "
            "when the Reel is shared there."
        )
    return None


def prepare_cover(path: Path, max_bytes: int) -> CoverAsset:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ReelpostError(f"Thumbnail not found: {path}")
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:
        raise ReelpostError(f"Could not read thumbnail {path.name}: {exc}") from exc
    with image:
        if _can_use_original(path, image, max_bytes):
            width, height = image.size
            return CoverAsset(path, width, height, None, False)
        rgb = _to_rgb(image)
        width, height = rgb.size
        if width <= 0 or height <= 0:
            raise ReelpostError(f"Thumbnail {path.name} has no visible pixels.")
        temporary = Path(tempfile.mkdtemp(prefix="reelpost-cover-"))
        dest = temporary / "cover.jpg"
        try:
            _save_under(rgb, dest, max_bytes)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return CoverAsset(dest, width, height, temporary, True)


def cover_from_url(url: str, max_bytes: int, *, session: requests.Session | None = None) -> CoverAsset:
    if not url.lower().startswith("https://"):
        raise ReelpostError("Thumbnail URL must start with https://.")
    download_dir = Path(tempfile.mkdtemp(prefix="reelpost-cover-"))
    try:
        downloaded = _download(url, download_dir, session or requests.Session())
        asset = prepare_cover(downloaded, max_bytes)
    except Exception:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise
    if asset.temporary_dir is None:
        asset.temporary_dir = download_dir
        asset.reencoded = False
    else:
        shutil.rmtree(download_dir, ignore_errors=True)
    return asset


def _download(url: str, directory: Path, session: requests.Session) -> Path:
    dest: Path
    try:
        response = session.get(url, stream=True, timeout=(30, 60), allow_redirects=True)
    except requests.RequestException as exc:
        raise ReelpostError(f"Could not download the thumbnail: {exc}") from exc
    with response:
        final = response.url or url
        if not final.lower().startswith("https://"):
            raise ReelpostError("Thumbnail URL redirected away from HTTPS.")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ReelpostError(f"Thumbnail URL returned HTTP {response.status_code}.") from exc
        size = 0
        suffix = Path(urlparse(final).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".img"
        dest = directory / f"download{suffix}"
        with dest.open("wb") as handle:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > FB_COVER_MAX_BYTES:
                    raise ReelpostError("Thumbnail URL is larger than 10 MB.")
                handle.write(chunk)
    if size <= 0:
        raise ReelpostError("Thumbnail URL was empty.")
    return dest


def _can_use_original(path: Path, image: Image.Image, max_bytes: int) -> bool:
    size = path.stat().st_size
    return path.suffix.lower() in {".jpg", ".jpeg"} and image.mode == "RGB" and 0 < size <= max_bytes


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _save_under(image: Image.Image, dest: Path, max_bytes: int) -> None:
    last = 0
    for quality in (90, 80, 70, 60, 50):
        image.save(dest, "JPEG", quality=quality, optimize=True)
        last = dest.stat().st_size
        if last <= max_bytes:
            return
    raise ReelpostError(
        f"Thumbnail is still {format_bytes(last)} after JPEG compression. "
        f"The limit is {format_bytes(max_bytes)}."
    )


def _within(duration: float, low: float, high: float) -> bool:
    return (low - 0.05) <= duration <= (high + 0.05)


def _first(boxes: list[tuple[bytes, int, int]], typ: bytes) -> int | None:
    for index, (found, _start, _end) in enumerate(boxes):
        if found == typ:
            return index
    return None


def _find_duration(handle, start: int, end: int) -> float | None:
    for typ, box_start, box_end in _iter_boxes(handle, start, end):
        if typ == b"mvhd":
            payload = _read(handle, box_start, box_end)
            return None if payload is None else _parse_mvhd(payload)
    return None


def _find_dimensions(handle, start: int, end: int) -> tuple[int | None, int | None]:
    best: tuple[int, int] | None = None
    for typ, box_start, box_end in _iter_boxes(handle, start, end):
        if typ != b"trak":
            continue
        for child, child_start, child_end in _iter_boxes(handle, box_start, box_end):
            if child != b"tkhd":
                continue
            payload = _read(handle, child_start, child_end)
            parsed = None if payload is None else _parse_tkhd(payload)
            if parsed is None:
                continue
            width, height = parsed
            if best is None or width * height > best[0] * best[1]:
                best = (width, height)
    if best is None:
        return None, None
    return best


def _parse_mvhd(payload: bytes) -> float | None:
    if len(payload) < 20:
        return None
    version = payload[0]
    if version == 0:
        timescale, duration = struct.unpack_from(">II", payload, 12)
    elif version == 1:
        if len(payload) < 32:
            return None
        timescale = struct.unpack_from(">I", payload, 20)[0]
        duration = struct.unpack_from(">Q", payload, 24)[0]
    else:
        return None
    if timescale <= 0 or duration <= 0:
        return None
    return duration / timescale


def _parse_tkhd(payload: bytes) -> tuple[int, int] | None:
    if not payload:
        return None
    version = payload[0]
    if version == 0 and len(payload) >= 84:
        offset = 76
    elif version == 1 and len(payload) >= 96:
        offset = 88
    else:
        return None
    width = struct.unpack_from(">I", payload, offset)[0] / 65536
    height = struct.unpack_from(">I", payload, offset + 4)[0] / 65536
    if width < 1 or height < 1:
        return None
    return int(round(width)), int(round(height))


def _read(handle, start: int, end: int, limit: int = 1024 * 1024) -> bytes | None:
    length = end - start
    if length < 0 or length > limit:
        return None
    handle.seek(start)
    data = handle.read(length)
    if len(data) != length:
        return None
    return data


def _iter_boxes(handle, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        handle.seek(pos)
        header = handle.read(8)
        if len(header) < 8:
            return
        size, typ = struct.unpack(">I4s", header)
        header_len = 8
        if size == 1:
            extra = handle.read(8)
            if len(extra) < 8:
                return
            size = struct.unpack(">Q", extra)[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len or pos + size > end:
            return
        yield typ, pos + header_len, pos + size
        pos += size
