import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from fakes import ScriptedSession
from reelpost.facebook import publish_facebook
from reelpost.graph import GraphClient
from reelpost.instagram import publish_instagram


def _client():
    session = ScriptedSession()
    return GraphClient("page-token", session=session), session


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class InstagramTests(unittest.TestCase):
    def test_publishes_a_reel_with_caption_and_cover(self):
        client, session = _client()
        session.push({"id": "container-1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1"})
        session.push({"success": True})
        session.push({"status_code": "FINISHED", "status": "ready", "video_status": {}})
        session.push({"id": "media-1"})
        session.push({"status_code": "PUBLISHED", "status": "done", "video_status": {}})
        session.push({"permalink": "https://www.instagram.com/reel/abc/"})
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"0123456789")
            result = publish_instagram(
                client,
                "ig-1",
                video,
                "Morning\n\n#berlin",
                cover_url="https://cdn.example/cover.jpg",
                collaborators=["natgeo"],
                sleep=clock.sleep,
                now=clock.now,
            )
        self.assertEqual(result["media_id"], "media-1")
        self.assertEqual(result["permalink"], "https://www.instagram.com/reel/abc/")
        created = session.calls[0]
        self.assertEqual(created["data"]["media_type"], "REELS")
        self.assertEqual(created["data"]["caption"], "Morning\n\n#berlin")
        self.assertEqual(created["data"]["cover_url"], "https://cdn.example/cover.jpg")
        self.assertEqual(created["data"]["share_to_feed"], "true")
        self.assertEqual(created["data"]["collaborators"], '["natgeo"]')
        upload = session.calls[1]
        self.assertEqual(upload["headers"]["Authorization"], "OAuth page-token")
        self.assertEqual(upload["headers"]["offset"], "0")
        self.assertEqual(upload["data"], b"0123456789")

    def test_resumes_after_a_dropped_connection(self):
        client, session = _client()
        session.push({"id": "container-1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1"})
        session.push_exc(requests.ConnectionError("reset"))
        session.push({"video_status": {"uploading_phase": {"bytes_transferred": 4}}})
        session.push({"success": True})
        session.push({"status_code": "FINISHED", "video_status": {}})
        session.push({"id": "media-1"})
        session.push({"status_code": "PUBLISHED", "video_status": {}})
        session.push({})
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"0123456789")
            publish_instagram(client, "ig-1", video, "Hi", sleep=clock.sleep, now=clock.now, timeout=30)
        uploads = [call for call in session.calls if call["method"] == "POST" and "rupload" in call["url"]]
        self.assertEqual(uploads[1]["headers"]["offset"], "4")
        self.assertEqual(uploads[1]["data"], b"456789")


class FacebookTests(unittest.TestCase):
    def _ready(self):
        return {
            "status": {
                "video_status": "ready",
                "processing_phase": {"status": "complete"},
                "uploading_phase": {"status": "complete"},
            }
        }

    def test_publishes_with_description_title_and_cover(self):
        client, session = _client()
        session.push({"video_id": "99", "upload_url": "https://rupload.facebook.com/video-upload/99"})
        session.push({"success": True})
        session.push({"success": True, "post_id": "99_1"})
        session.push(self._ready())
        session.push({"success": True})
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            cover = Path(tmp) / "cover.jpg"
            video.write_bytes(b"video-bytes")
            cover.write_bytes(b"jpeg-bytes")
            result = publish_facebook(
                client,
                "page-1",
                video,
                "Morning #berlin",
                title="Morning",
                thumbnail=cover,
                sleep=clock.sleep,
                now=clock.now,
            )
        self.assertEqual(result["video_id"], "99")
        self.assertTrue(result["thumbnail"])
        self.assertEqual(result["permalink"], "https://www.facebook.com/reel/99")
        upload = next(call for call in session.calls if "rupload" in call["url"])
        self.assertEqual(upload["url"], "https://rupload.facebook.com/video-upload/v26.0/99")
        finish = next(call for call in session.calls if call["data"] and isinstance(call["data"], dict) and call["data"].get("upload_phase") == "finish")
        self.assertEqual(finish["data"]["description"], "Morning #berlin")
        self.assertEqual(finish["data"]["title"], "Morning")
        self.assertEqual(finish["data"]["video_state"], "PUBLISHED")
        thumb = session.calls[-1]
        self.assertIn("/99/thumbnails", thumb["url"])
        self.assertEqual(thumb["files"]["source"], b"jpeg-bytes")
        self.assertEqual(thumb["form"]["is_preferred"], "true")

    def test_schedule_uses_a_unix_timestamp(self):
        client, session = _client()
        session.push({"video_id": "99", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/99"})
        session.push({"success": True})
        session.push({"success": True})
        session.push(self._ready())
        when = datetime.now(timezone.utc) + timedelta(hours=2)
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"video-bytes")
            publish_facebook(
                client,
                "page-1",
                video,
                "Later",
                schedule_at=when,
                sleep=clock.sleep,
                now=clock.now,
            )
        finish = next(call for call in session.calls if isinstance(call["data"], dict) and call["data"].get("upload_phase") == "finish")
        self.assertEqual(finish["data"]["video_state"], "SCHEDULED")
        self.assertEqual(finish["data"]["scheduled_publish_time"], str(int(when.timestamp())))

    def test_processing_error_stops_before_the_cover(self):
        client, session = _client()
        session.push({"video_id": "99"})
        session.push({"success": True})
        session.push({"success": True})
        session.push(
            {
                "status": {
                    "video_status": "processing",
                    "processing_phase": {"status": "error", "error": {"message": "Resolution too low."}},
                }
            }
        )
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            cover = Path(tmp) / "cover.jpg"
            video.write_bytes(b"video-bytes")
            cover.write_bytes(b"jpeg")
            with self.assertRaises(Exception) as caught:
                publish_facebook(
                    client,
                    "page-1",
                    video,
                    "Hi",
                    thumbnail=cover,
                    sleep=clock.sleep,
                    now=clock.now,
                    timeout=5,
                )
        self.assertIn("Resolution too low", str(caught.exception))
        self.assertFalse(any("thumbnails" in call["url"] for call in session.calls))
