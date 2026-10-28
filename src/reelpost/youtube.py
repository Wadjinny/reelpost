"""YouTube Shorts through the Data API: resumable upload, then a thumbnail."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from reelpost.constants import (
    YT_CATEGORY_ID,
    YT_DESCRIPTION_BYTES,
    YT_REDIRECT_URI,
    YT_SCOPES,
    YT_TAGS_LIMIT,
    YT_TITLE_LIMIT,
)
from reelpost.errors import ReelpostError
from reelpost.graph import FileSlice

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

_SHORTS = re.compile(r"(?i)(?:^|\s)#shorts\b")
_TRAILING_TAGS = re.compile(r"(?:\s+#[\w]+)+\s*$")
_RANGE = re.compile(r"bytes=(\d+)-(\d+)")


class _UploadFinished(Exception):
    def __init__(self, body: dict):
        self.body = body


def build_youtube_login_url(client_id: str, state: str, redirect_uri: str = YT_REDIRECT_URI) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YT_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_youtube_code(
    session: requests.Session,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = YT_REDIRECT_URI,
) -> dict:
    return _token_request(
        session,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )


def refresh_youtube_token(session: requests.Session, client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _token_request(
        session,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )


def token_expiry(expires_in) -> str:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 3600
    if seconds <= 0:
        seconds = 3600
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def access_is_fresh(token: str | None, expires_at: str | None, *, skew: int = 60) -> bool:
    if not token:
        return False
    if not expires_at:
        return True
    try:
        when = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > datetime.now(timezone.utc) + timedelta(seconds=skew)


def fetch_channel(session: requests.Session, access_token: str) -> tuple[str | None, str | None]:
    try:
        response = session.get(
            CHANNEL_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=(30, 60),
        )
    except requests.RequestException:
        return None, None
    if response.status_code >= 400:
        return None, None
    body = _json_object(response)
    items = body.get("items") or []
    if not items:
        return None, None
    snippet = items[0].get("snippet") or {}
    return items[0].get("id"), snippet.get("title")


def youtube_title(explicit: str | None, caption: str, filename: str) -> tuple[str, str | None]:
    """A YouTube title of at most 100 characters, with < and > removed."""
    if explicit and explicit.strip():
        title = explicit.replace("<", "").replace(">", "").strip()
        if not title:
            raise ReelpostError("The YouTube title is empty after removing < and >.")
        if len(title) > YT_TITLE_LIMIT:
            raise ReelpostError(
                f"YouTube titles must be {YT_TITLE_LIMIT} characters or fewer. This one is {len(title)}."
            )
        return title, None
    first = caption.split("\n", 1)[0].strip()
    first = _TRAILING_TAGS.sub("", first).strip()
    first = first.replace("<", "").replace(">", "").strip()
    if not first:
        first = Path(filename).stem.replace("_", " ").strip() or "Short"
        first = first.replace("<", "").replace(">", "").strip() or "Short"
    if len(first) <= YT_TITLE_LIMIT:
        return first, None
    shortened = first[: YT_TITLE_LIMIT - 1].rstrip() + "…"
    return shortened, f"The YouTube title was shortened to {YT_TITLE_LIMIT} characters."


def youtube_description(caption: str) -> str:
    """The shared caption, plus #Shorts when it is not already there."""
    text = caption.strip()
    if not _SHORTS.search(text):
        text = f"{text}\n\n#Shorts".strip()
    size = len(text.encode("utf-8"))
    if size > YT_DESCRIPTION_BYTES:
        raise ReelpostError(
            f"The YouTube description is {size} bytes. The limit is {YT_DESCRIPTION_BYTES}."
        )
    return text


def youtube_tags(tags: list[str]) -> tuple[list[str], str | None]:
    """Keyword tags, staying inside YouTube's 500-character budget."""
    kept: list[str] = []
    used = 0
    dropped = False
    for tag in tags:
        clean = tag.replace("<", "").replace(">", "").strip()
        if not clean:
            continue
        if used + len(clean) > YT_TAGS_LIMIT:
            dropped = True
            continue
        kept.append(clean)
        used += len(clean)
    warning = None
    if dropped:
        warning = (
            "Some tags were left off YouTube because keyword tags are limited to 500 characters. "
            "They stay in the description."
        )
    return kept, warning


def publish_youtube(
    session,
    access_token: str,
    video: Path,
    caption: str,
    *,
    title: str,
    tags: list[str],
    thumbnail: Path | None = None,
    privacy: str = "public",
    schedule_at: datetime | None = None,
    ai_generated: bool = False,
    log=None,
    on_bytes=None,
) -> dict:
    speak = log or (lambda _message: None)
    description = youtube_description(caption)
    keywords, _warning = youtube_tags(tags)
    effective_privacy = "private" if schedule_at is not None else privacy
    metadata = _metadata(title, description, keywords, effective_privacy, schedule_at, ai_generated)
    mime = "video/quicktime" if video.suffix.lower() == ".mov" else "video/mp4"
    size = video.stat().st_size
    if size <= 0:
        raise ReelpostError("The video file is empty.")
    speak("starting the YouTube upload")
    location = _start_session(session, access_token, metadata, size, mime)
    body = _send_video(session, location, access_token, video, mime, size, on_bytes)
    video_id = str(body.get("id") or "")
    if not video_id:
        raise ReelpostError("YouTube did not return a video id.")
    cover_set = False
    if thumbnail is not None:
        speak("uploading the YouTube cover")
        set_youtube_thumbnail(session, access_token, video_id, thumbnail)
        cover_set = True
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
        "thumbnail": cover_set,
        "privacy": effective_privacy,
        "scheduled": schedule_at is not None,
        "publish_at": None if schedule_at is None else schedule_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def set_youtube_thumbnail(session, access_token: str, video_id: str, path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            response = session.post(
                THUMBNAIL_URL,
                params={"videoId": video_id, "uploadType": "media"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "image/jpeg",
                },
                data=handle,
                timeout=(30, 120),
                allow_redirects=False,
            )
    except requests.RequestException as exc:
        raise ReelpostError(
            f"The Short was published as {video_id}, but the cover was not set. {exc} "
            f"Retry with: reelpost thumbnail {video_id} --to youtube --thumbnail COVER.jpg --yes"
        ) from exc
    if response.status_code >= 400:
        message = _google_message(response, access_token)
        if response.status_code == 403:
            message += " Custom thumbnails need a phone-verified YouTube channel."
        raise ReelpostError(
            f"The Short was published as {video_id}, but the cover was not set. {message} "
            f"Retry with: reelpost thumbnail {video_id} --to youtube --thumbnail COVER.jpg --yes"
        )
    return _json_object(response)


def _metadata(title, description, tags, privacy, schedule_at, ai_generated) -> dict:
    snippet = {"title": title, "description": description, "categoryId": YT_CATEGORY_ID}
    if tags:
        snippet["tags"] = tags
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if schedule_at is not None:
        status["publishAt"] = schedule_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if ai_generated:
        status["containsSyntheticMedia"] = True
    return {"snippet": snippet, "status": status}


def _start_session(session, access_token: str, metadata: dict, size: int, mime: str) -> str:
    try:
        response = session.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime,
            },
            json=metadata,
            timeout=(30, 60),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ReelpostError(f"Could not start the YouTube upload: {exc}") from exc
    if response.status_code >= 400:
        raise ReelpostError(f"YouTube rejected the upload. {_google_message(response, access_token)}")
    location = _header(getattr(response, "headers", None), "Location")
    if not location:
        raise ReelpostError("YouTube did not return an upload URL.")
    return location


def _send_video(session, location, access_token, video, mime, size, on_bytes) -> dict:
    offset = 0
    last_error: Exception | None = None
    for _attempt in range(1, 5):
        try:
            response = _put_slice(session, location, access_token, video, mime, offset, size, on_bytes)
        except requests.RequestException as exc:
            last_error = exc
            try:
                offset = _query_offset(session, location, access_token, size)
            except _UploadFinished as done:
                return done.body
            continue
        if response.status_code in (200, 201):
            body = _json_object(response)
            if body.get("id"):
                return body
            raise ReelpostError("YouTube finished the upload without a video id.")
        if response.status_code == 308:
            offset = _offset_from_range(_header(getattr(response, "headers", None), "Range"), 0)
            if offset >= size:
                return _completed_upload(session, location, access_token, size)
            continue
        if response.status_code >= 500:
            continue
        raise ReelpostError(f"YouTube rejected the video upload. {_google_message(response, access_token)}")
    detail = f" {last_error}" if last_error else ""
    raise ReelpostError(f"YouTube upload failed after retries.{detail}")


def _put_slice(session, location, access_token, video, mime, start, total, on_bytes):
    length = total - start
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": mime,
    }
    if start:
        headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
    with FileSlice(video, start, length, on_bytes=on_bytes, base=start, total=total) as reader:
        return session.put(
            location,
            data=reader,
            headers=headers,
            timeout=(30, 300),
            allow_redirects=False,
        )


def _query_offset(session, location, access_token, total) -> int:
    response = session.put(
        location,
        data=b"",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Range": f"bytes */{total}",
        },
        timeout=(30, 60),
        allow_redirects=False,
    )
    if response.status_code in (200, 201):
        raise _UploadFinished(_json_object(response))
    if response.status_code == 308:
        return _offset_from_range(_header(getattr(response, "headers", None), "Range"), 0)
    return 0


def _completed_upload(session, location, access_token, total) -> dict:
    try:
        offset = _query_offset(session, location, access_token, total)
    except _UploadFinished as done:
        return done.body
    raise ReelpostError(f"YouTube accepted {offset} bytes but did not finish the upload.")


def _offset_from_range(header: str | None, fallback: int) -> int:
    if not header:
        return fallback
    match = _RANGE.search(header)
    if not match:
        return fallback
    return int(match.group(2)) + 1


def _token_request(session, data: dict) -> dict:
    try:
        response = session.post(TOKEN_URL, data=data, timeout=(30, 60))
    except requests.RequestException as exc:
        raise ReelpostError(f"Could not reach Google to finish login: {exc}") from exc
    body = _json_object(response)
    if response.status_code >= 400 or "access_token" not in body:
        message = body.get("error_description") or body.get("error") or "Google did not return a token."
        raise ReelpostError(f"Google did not return a token. {message}")
    return body


def _json_object(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _google_message(response, token: str) -> str:
    body = _json_object(response)
    error = body.get("error")
    message = ""
    reason = ""
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
    elif isinstance(error, str):
        message = error
    if not message:
        message = f"HTTP {response.status_code}"
    if reason and reason not in message:
        message = f"{message} ({reason})"
    if reason in {"quotaExceeded", "uploadRateLimitExceeded", "dailyLimitExceeded"}:
        message += " The default YouTube Data API quota allows about 6 video uploads a day."
    if token and token in message:
        message = message.replace(token, "[token]")
    return message


def _header(headers, name: str) -> str | None:
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value:
        return value
    return getter(name.lower()) or getter(name.title())
