"""Graph API client and resumable binary uploads."""

from __future__ import annotations

import re
from pathlib import Path

import requests

from reelpost.constants import DEFAULT_GRAPH_VERSION
from reelpost.errors import GraphAPIError, ReelpostError, UploadError

_VERSION = re.compile(r"v\d+\.\d+")
_UPLOAD_VERSION = re.compile(r"/video-upload/v\d+\.\d+/")


class FileSlice:
    """A file range with a known length, so the upload sets Content-Length."""

    def __init__(self, path: Path, start: int, length: int, on_bytes=None, base: int = 0, total: int | None = None):
        self._handle = path.open("rb")
        self._handle.seek(start)
        self._remaining = length
        self._length = length
        self._sent = 0
        self._on_bytes = on_bytes
        self._base = base
        self._total = total if total is not None else length

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        chunk = self._handle.read(size)
        if not chunk:
            self._remaining = 0
            return b""
        self._remaining -= len(chunk)
        self._sent += len(chunk)
        if self._on_bytes is not None:
            self._on_bytes(self._base + self._sent, self._total)
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "FileSlice":
        return self

    def __exit__(self, *args) -> None:
        self.close()


class GraphClient:
    def __init__(self, token: str, version: str = DEFAULT_GRAPH_VERSION, session: requests.Session | None = None):
        if not token:
            raise ReelpostError("Missing access token.")
        self.token = token
        self.version = normalize_version(version)
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "reelpost/0.1.0")
        self.graph = f"https://graph.facebook.com/{self.version}"

    def get(self, path: str, params: dict | None = None) -> dict:
        response = self.session.get(
            self._url(path),
            params=params,
            headers=self._bearer(),
            timeout=(30, 60),
        )
        return self._parse(response)

    def get_url(self, url: str) -> dict:
        response = self.session.get(url, headers=self._bearer(), timeout=(30, 60))
        return self._parse(response)

    def post(self, path: str, data: dict) -> dict:
        response = self.session.post(
            self._url(path),
            data=data,
            headers=self._bearer(),
            timeout=(30, 120),
        )
        return self._parse(response)

    def post_files(self, path: str, data: dict, files: dict) -> dict:
        form = dict(data)
        form["access_token"] = self.token
        response = self.session.post(
            self._url(path),
            data=form,
            files=files,
            headers=self._bearer(),
            timeout=(30, 120),
        )
        return self._parse(response)

    def upload(self, url: str, path: Path, *, offset: int = 0, on_bytes=None, timeout=(30, 1800)) -> dict:
        size = path.stat().st_size
        if offset < 0 or offset > size:
            raise UploadError(f"Upload offset {offset} is outside a file of {size} bytes.")
        length = size - offset
        headers = {
            "Authorization": f"OAuth {self.token}",
            "offset": str(offset),
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        }
        reader = FileSlice(path, offset, length, on_bytes=on_bytes, base=offset, total=size)
        try:
            response = self.session.post(url, data=reader, headers=headers, timeout=timeout)
        except requests.RequestException:
            raise
        finally:
            reader.close()
        return self._parse_upload(response)

    def collect(self, path: str, params: dict) -> list[dict]:
        query = dict(params)
        query.setdefault("limit", 100)
        body = self.get(path, query)
        rows = list(body.get("data") or [])
        seen = 0
        next_url = ((body.get("paging") or {}).get("next"))
        while next_url and seen < 10:
            seen += 1
            body = self.get_url(next_url)
            rows.extend(body.get("data") or [])
            next_url = ((body.get("paging") or {}).get("next"))
        return rows

    def _url(self, path: str) -> str:
        if path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.graph + path

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _parse(self, response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            err = body["error"] or {}
            message = str(err.get("message") or "Graph API error")
            trace = err.get("fbtrace_id")
            if trace:
                message = f"{message} (fbtrace_id {trace})"
            code = err.get("code")
            if code in {4, 17, 32, 613}:
                message = f"Meta rate limit. {message}"
            raise GraphAPIError(self._redact(message), code=code, subcode=err.get("error_subcode"))
        if response.status_code >= 400 or body is None:
            text = self._redact((response.text or "")[:500])
            raise GraphAPIError(
                self._redact(f"HTTP {response.status_code} from Graph API. {text}".strip()),
                code=response.status_code if response.status_code >= 400 else None,
            )
        if not isinstance(body, dict):
            raise GraphAPIError("Graph API returned a response that was not an object.")
        return body

    def _parse_upload(self, response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("success") is True:
            return body
        if isinstance(body, dict) and "error" in body:
            self._parse(response)
        debug = body.get("debug_info") if isinstance(body, dict) else None
        debug = debug or {}
        message = None
        if isinstance(body, dict):
            message = debug.get("message") or body.get("message")
        if not message:
            message = self._redact((response.text or "")[:500]) or f"HTTP {response.status_code}"
        retriable = bool(debug.get("retriable")) or response.status_code >= 500
        raise UploadError(self._redact(str(message)), retriable=retriable)

    def _redact(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "[redacted]")
        return text


def normalize_version(value: str | None) -> str:
    text = (value or DEFAULT_GRAPH_VERSION).strip()
    if not text.startswith("v"):
        text = "v" + text
    if not _VERSION.fullmatch(text):
        raise ReelpostError(f"Invalid Graph version {value!r}. Example: v26.0")
    return text


def facebook_upload_url(video_id: str, upload_url: str | None, version: str) -> str:
    if upload_url and _UPLOAD_VERSION.search(upload_url):
        return upload_url
    return f"https://rupload.facebook.com/video-upload/{version}/{video_id}"


def bytes_from_video_status(video_status: dict | None) -> int | None:
    if not isinstance(video_status, dict):
        return None
    phase = video_status.get("uploading_phase") or {}
    if not isinstance(phase, dict):
        return None
    for key in ("bytes_transferred", "bytes_transfered"):
        if phase.get(key) is not None:
            try:
                return int(phase[key])
            except (TypeError, ValueError):
                return None
    return None


def upload_with_resume(client: GraphClient, url: str, path: Path, read_offset, *, attempts: int = 3, log=None, on_bytes=None) -> dict:
    """Upload a local file. On a dropped connection, continue from the bytes Meta already has."""
    log = log or (lambda _message: None)
    size = path.stat().st_size
    offset = 0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            log(f"uploading {path.name} from byte {offset} of {size} (attempt {attempt}/{attempts})")
            return client.upload(url, path, offset=offset, on_bytes=on_bytes)
        except (UploadError, requests.RequestException) as exc:
            last = exc
            retriable = isinstance(exc, requests.RequestException) or getattr(exc, "retriable", False)
            if not retriable or attempt == attempts:
                raise ReelpostError(str(exc)) from exc
            transferred = _safe_offset(read_offset)
            if transferred is None:
                continue
            if transferred >= size:
                return {"success": True, "message": "Upload already complete."}
            offset = max(0, transferred)
    raise ReelpostError(str(last or "Upload failed."))


def is_temporary(exc: Exception) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    if isinstance(exc, GraphAPIError):
        return exc.code in {1, 2} or (isinstance(exc.code, int) and exc.code >= 500)
    return False


def _safe_offset(read_offset) -> int | None:
    try:
        value = read_offset()
    except (ReelpostError, requests.RequestException):
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
