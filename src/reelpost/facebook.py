"""Facebook Page Reels through the Video API."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from reelpost.errors import GraphAPIError, ReelpostError
from reelpost.graph import (
    GraphClient,
    bytes_from_video_status,
    facebook_upload_url,
    is_temporary,
    upload_with_resume,
)
from reelpost.schedule import assert_schedule_window


def publish_facebook(
    client: GraphClient,
    page_id: str,
    video: Path,
    description: str,
    *,
    title: str | None = None,
    thumbnail: Path | None = None,
    place_id: str | None = None,
    schedule_at: datetime | None = None,
    ai_generated: bool = False,
    timeout: float = 600,
    log=None,
    on_bytes=None,
    sleep=time.sleep,
    now=time.monotonic,
    moment: datetime | None = None,
) -> dict:
    log = log or (lambda _message: None)
    if schedule_at is not None:
        assert_schedule_window(schedule_at, moment or datetime.now(timezone.utc))

    log("starting the Facebook Reel upload")
    started = client.post(f"/{page_id}/video_reels", {"upload_phase": "start"})
    video_id = str(started.get("video_id") or "")
    if not video_id:
        raise ReelpostError("Facebook did not return a video id.")
    upload_url = facebook_upload_url(video_id, started.get("upload_url"), client.version)

    def read_offset() -> int | None:
        body = client.get(f"/{video_id}", {"fields": "status"})
        return _transferred(body)

    upload_with_resume(client, upload_url, video, read_offset, log=log, on_bytes=on_bytes)
    finish = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
    }
    if description:
        finish["description"] = description
    if schedule_at is not None:
        finish["video_state"] = "SCHEDULED"
        finish["scheduled_publish_time"] = str(int(schedule_at.timestamp()))
    if title:
        finish["title"] = title
    if place_id:
        finish["place"] = str(place_id)
    if ai_generated:
        finish["is_ai_generated"] = "true"
    log(f"publishing Facebook Reel {video_id}")
    result = client.post(f"/{page_id}/video_reels", finish)
    if result.get("success") is False:
        raise ReelpostError(result.get("message") or f"Facebook did not accept Reel {video_id}.")
    log("waiting for Facebook to process the video")
    _poll_processed(client, video_id, timeout=timeout, log=log, sleep=sleep, now=now)
    if thumbnail is not None:
        log("uploading the Facebook cover")
        upload_thumbnail(client, video_id, thumbnail, sleep=sleep)
    return {
        "video_id": video_id,
        "post_id": result.get("post_id"),
        "thumbnail": thumbnail is not None,
        "permalink": f"https://www.facebook.com/reel/{video_id}",
        "scheduled": schedule_at is not None,
    }


def upload_thumbnail(client: GraphClient, video_id: str, path: Path, *, attempts: int = 3, sleep=time.sleep) -> dict:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with path.open("rb") as handle:
                return client.post_files(
                    f"/{video_id}/thumbnails",
                    {"is_preferred": "true"},
                    {"source": ("cover.jpg", handle, "image/jpeg")},
                )
        except GraphAPIError as exc:
            last = exc
            if attempt == attempts:
                raise ReelpostError(
                    f"The Reel was published as video {video_id}, but the cover was not set. {exc} "
                    f"Retry with: reelpost thumbnail {video_id} --thumbnail COVER.jpg"
                ) from exc
            sleep(5)
    raise ReelpostError(str(last or "Cover upload failed."))


def _poll_processed(client, video_id, *, timeout, log, sleep, now, interval: float = 5) -> dict:
    deadline = now() + timeout
    last_text = None
    while True:
        body = None
        try:
            body = client.get(f"/{video_id}", {"fields": "status"})
        except (GraphAPIError, requests.RequestException) as exc:
            if not is_temporary(exc):
                raise
            log(f"facebook video {video_id}: temporary error ({exc})")
        if body is not None:
            status = _status_dict(body)
            text = _describe(status)
            if text != last_text:
                log(f"facebook video {video_id}: {text}")
                last_text = text
            problem = _failure(status)
            if problem:
                raise ReelpostError(problem)
            if _processed(status):
                return body
        if now() >= deadline:
            raise ReelpostError(
                f"Timed out waiting for Facebook to process video {video_id}. "
                "It may still finish. Check the Page before uploading it again. "
                f"Last status: {last_text or 'unavailable'}."
            )
        sleep(interval)


def _status_dict(body: dict) -> dict:
    status = body.get("status")
    if isinstance(status, dict):
        return status
    return body


def _transferred(body: dict) -> int | None:
    return bytes_from_video_status(_status_dict(body))


def _describe(status: dict) -> str:
    video_status = status.get("video_status") or ""
    processing = (status.get("processing_phase") or {}).get("status") or ""
    publishing = (status.get("publishing_phase") or {}).get("status") or ""
    parts = [str(part) for part in (video_status, processing, publishing) if part]
    return " / ".join(parts) or "waiting"


def _failure(status: dict) -> str | None:
    if str(status.get("video_status") or "").lower() == "error":
        return _phase_message(status) or "Facebook video processing failed."
    for key in ("uploading_phase", "processing_phase", "publishing_phase"):
        phase = status.get(key) or {}
        if not isinstance(phase, dict):
            continue
        if str(phase.get("status") or "").lower() == "error":
            return _phase_message(status) or f"Facebook {key.replace('_', ' ')} failed."
        err = phase.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return None


def _phase_message(status: dict) -> str | None:
    for key in ("processing_phase", "uploading_phase", "publishing_phase"):
        phase = status.get(key) or {}
        if not isinstance(phase, dict):
            continue
        err = phase.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return None


def _processed(status: dict) -> bool:
    processing = str((status.get("processing_phase") or {}).get("status") or "").lower()
    video_status = str(status.get("video_status") or "").lower()
    return processing == "complete" or video_status in {"ready", "published"}

