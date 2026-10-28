"""Instagram Reels through the Content Publishing API."""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from reelpost.errors import GraphAPIError, ReelpostError
from reelpost.graph import GraphClient, bytes_from_video_status, is_temporary, upload_with_resume


def read_quota(client: GraphClient, ig_user_id: str) -> tuple[int, int] | None:
    try:
        body = client.get(
            f"/{ig_user_id}/content_publishing_limit",
            {"fields": "quota_usage,config"},
        )
    except GraphAPIError:
        return None
    rows = body.get("data") or []
    if not rows:
        return None
    row = rows[0]
    config = row.get("config") or {}
    try:
        return int(row["quota_usage"]), int(config["quota_total"])
    except (KeyError, TypeError, ValueError):
        return None


def publish_instagram(
    client: GraphClient,
    ig_user_id: str,
    video: Path,
    caption: str,
    *,
    cover_url: str | None = None,
    thumb_offset_ms: int | None = None,
    share_to_feed: bool = True,
    collaborators: list[str] | None = None,
    location_id: str | None = None,
    ai_generated: bool = False,
    timeout: float = 600,
    log=None,
    on_bytes=None,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    """Create a Reel container, upload the local file, and publish it.

    A Page access token for the Page linked to the professional account is the
    token Meta documents for Instagram API with Facebook Login.
    """
    log = log or (lambda _message: None)
    data: dict[str, str] = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "share_to_feed": "true" if share_to_feed else "false",
    }
    if caption:
        data["caption"] = caption
    if cover_url:
        data["cover_url"] = cover_url
    elif thumb_offset_ms is not None:
        data["thumb_offset"] = str(int(thumb_offset_ms))
    if collaborators:
        data["collaborators"] = json.dumps(collaborators, separators=(",", ":"))
    if location_id:
        data["location_id"] = str(location_id)
    if ai_generated:
        data["is_ai_generated"] = "true"

    log("creating the Instagram Reel container")
    created = client.post(f"/{ig_user_id}/media", data)
    container_id = str(created.get("id") or "")
    if not container_id:
        raise ReelpostError("Instagram did not return a media container id.")
    upload_url = created.get("uri") or (
        f"https://rupload.facebook.com/ig-api-upload/{client.version}/{container_id}"
    )

    def read_offset() -> int | None:
        body = client.get(f"/{container_id}", {"fields": "video_status"})
        return bytes_from_video_status(body.get("video_status"))

    upload_with_resume(client, upload_url, video, read_offset, log=log, on_bytes=on_bytes)
    log("waiting for Instagram to finish processing the video")
    _poll_container(
        client,
        container_id,
        accept={"FINISHED"},
        timeout=timeout,
        log=log,
        sleep=sleep,
        now=now,
    )
    log("publishing the Instagram Reel")
    published = client.post(f"/{ig_user_id}/media_publish", {"creation_id": container_id})
    media_id = str(published.get("id") or "")
    if not media_id:
        raise ReelpostError(f"Instagram did not return a media id for container {container_id}.")
    confirmed = True
    try:
        _poll_container(
            client,
            container_id,
            accept={"PUBLISHED"},
            timeout=timeout,
            log=log,
            sleep=sleep,
            now=now,
        )
    except ReelpostError as exc:
        if str(exc).startswith("Timed out"):
            confirmed = False
            log(f"warning: {exc} Media id {media_id} was returned.")
        else:
            raise
    permalink = _permalink(client, media_id)
    return {
        "container_id": container_id,
        "media_id": media_id,
        "permalink": permalink,
        "confirmed": confirmed,
    }


def _permalink(client: GraphClient, media_id: str) -> str | None:
    try:
        info = client.get(f"/{media_id}", {"fields": "permalink"})
    except GraphAPIError:
        return None
    permalink = info.get("permalink")
    return str(permalink) if permalink else None


def _poll_container(client, container_id, *, accept, timeout, log, sleep, now, interval: float = 5) -> dict:
    deadline = now() + timeout
    last_text = None
    while True:
        body = None
        try:
            body = client.get(
                f"/{container_id}",
                {"fields": "id,status,status_code,video_status"},
            )
        except (GraphAPIError, requests.RequestException) as exc:
            if not is_temporary(exc):
                raise
            log(f"instagram container {container_id}: temporary error ({exc})")
        if body is not None:
            code = str(body.get("status_code") or "")
            text = " ".join(part for part in (code, str(body.get("status") or "")) if part).strip()
            if text != last_text:
                log(f"instagram container {container_id}: {text or 'waiting'}")
                last_text = text
            problem = _phase_error(body.get("video_status"))
            if code == "ERROR" or problem:
                raise ReelpostError(problem or body.get("status") or "Instagram processing failed.")
            if code == "EXPIRED":
                raise ReelpostError(f"Instagram media container {container_id} expired before it was published.")
            if code in accept:
                return body
        if now() >= deadline:
            raise ReelpostError(
                f"Timed out waiting for Instagram container {container_id} "
                f"(last status: {last_text or 'unavailable'})."
            )
        sleep(interval)


def _phase_error(video_status) -> str | None:
    if not isinstance(video_status, dict):
        return None
    for key in ("uploading_phase", "processing_phase", "publishing_phase"):
        phase = video_status.get(key) or {}
        if not isinstance(phase, dict):
            continue
        err = phase.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if str(phase.get("status") or "").lower() == "error":
            return str(phase.get("message") or f"Instagram {key.replace('_', ' ')} failed.")
    return None
