import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from fakes import ScriptedSession
from mp4util import write_movie
from reelpost.config import load_settings, save_settings
from reelpost.config import Settings
from reelpost.errors import ReelpostError
from reelpost.cli import ensure_youtube_token
from reelpost.youtube import publish_youtube, youtube_description, youtube_tags, youtube_title


class YoutubeMetadataTests(unittest.TestCase):
    def test_description_adds_shorts_once(self):
        self.assertEqual(youtube_description("Morning\n\n#berlin"), "Morning\n\n#berlin\n\n#Shorts")
        self.assertEqual(youtube_description("Morning #Shorts"), "Morning #Shorts")

    def test_title_comes_from_the_first_line_and_rejects_a_long_explicit_title(self):
        title, warning = youtube_title(None, "Morning\n\n#berlin", "clip.mp4")
        self.assertEqual(title, "Morning")
        self.assertIsNone(warning)
        with self.assertRaises(ReelpostError):
            youtube_title("x" * 101, "Morning", "clip.mp4")

    def test_keyword_tags_stop_at_500_characters(self):
        kept, warning = youtube_tags(["a" * 400, "b" * 200, "ok"])
        self.assertEqual(kept, ["a" * 400, "ok"])
        self.assertIn("500", warning or "")


class YoutubeUploadTests(unittest.TestCase):
    def test_upload_then_thumbnail(self):
        session = ScriptedSession()
        session.push({}, 200, {"Location": "https://upload.example/session"})
        session.push({"id": "abcdefghijk"}, 200)
        session.push({"items": []}, 200)
        with tempfile.TemporaryDirectory() as tmp:
            video = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=5000)
            cover = Path(tmp) / "cover.jpg"
            cover.write_bytes(b"jpeg")
            when = datetime.now(timezone.utc) + timedelta(hours=2)
            result = publish_youtube(
                session,
                "yt-token",
                video,
                "Morning\n\n#berlin",
                title="Morning",
                tags=["berlin"],
                thumbnail=cover,
                privacy="public",
                schedule_at=when,
                ai_generated=True,
            )
        self.assertEqual(result["video_id"], "abcdefghijk")
        self.assertEqual(result["privacy"], "private")
        self.assertTrue(result["thumbnail"])
        started = session.calls[0]
        self.assertEqual(started["data"]["snippet"]["title"], "Morning")
        self.assertIn("#Shorts", started["data"]["snippet"]["description"])
        self.assertEqual(started["data"]["snippet"]["tags"], ["berlin"])
        self.assertEqual(started["data"]["snippet"]["categoryId"], "22")
        self.assertEqual(started["data"]["status"]["privacyStatus"], "private")
        self.assertIn("publishAt", started["data"]["status"])
        self.assertTrue(started["data"]["status"]["containsSyntheticMedia"])
        self.assertFalse(started["data"]["status"]["selfDeclaredMadeForKids"])
        self.assertEqual(session.calls[1]["url"], "https://upload.example/session")
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer yt-token")
        self.assertEqual(session.calls[2]["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(session.calls[2]["params"]["videoId"], "abcdefghijk")

    def test_resumes_after_a_dropped_connection(self):
        session = ScriptedSession()
        session.push({}, 200, {"Location": "https://upload.example/session"})
        session.push_exc(requests.ConnectionError("reset"))
        session.push({}, 308, {"Range": "bytes=0-3"})
        session.push({"id": "abcdefghijk"}, 201)
        with tempfile.TemporaryDirectory() as tmp:
            video = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=5000)
            size = video.stat().st_size
            result = publish_youtube(
                session,
                "yt-token",
                video,
                "Morning",
                title="Morning",
                tags=[],
            )
        self.assertEqual(result["video_id"], "abcdefghijk")
        puts = [call for call in session.calls if call["method"] == "PUT"]
        self.assertNotIn("Content-Range", puts[0]["headers"])
        self.assertEqual(puts[1]["headers"]["Content-Range"], f"bytes */{size}")
        self.assertEqual(puts[1]["data"], b"")
        self.assertTrue(puts[2]["headers"]["Content-Range"].startswith(f"bytes 4-{size - 1}/{size}"))

    def test_thumbnail_forbidden_mentions_phone_verification(self):
        session = ScriptedSession()
        session.push({}, 200, {"Location": "https://upload.example/session"})
        session.push({"id": "abcdefghijk"}, 200)
        session.push({"error": {"message": "Forbidden", "errors": [{"reason": "forbidden"}]}}, 403)
        with tempfile.TemporaryDirectory() as tmp:
            video = write_movie(Path(tmp) / "clip.mp4", timescale=1000, duration=5000)
            cover = Path(tmp) / "cover.jpg"
            cover.write_bytes(b"jpeg")
            with self.assertRaises(ReelpostError) as caught:
                publish_youtube(
                    session,
                    "yt-token",
                    video,
                    "Morning",
                    title="Morning",
                    tags=[],
                    thumbnail=cover,
                )
        self.assertIn("phone-verified", str(caught.exception))
        self.assertIn("abcdefghijk", str(caught.exception))


class YoutubeTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._names = (
            "REELPOST_CONFIG_DIR",
            "REELPOST_YOUTUBE_CLIENT_ID",
            "REELPOST_YOUTUBE_CLIENT_SECRET",
            "REELPOST_YOUTUBE_REFRESH_TOKEN",
            "REELPOST_YOUTUBE_ACCESS_TOKEN",
            "REELPOST_YOUTUBE_CHANNEL_ID",
            "REELPOST_YOUTUBE_CHANNEL_TITLE",
        )
        self._saved = {name: os.environ.get(name) for name in self._names}
        os.environ["REELPOST_CONFIG_DIR"] = self.tmp.name
        for name in self._names:
            if name != "REELPOST_CONFIG_DIR":
                os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_missing_login_names_the_command(self):
        with self.assertRaises(ReelpostError) as caught:
            ensure_youtube_token(Settings(), ScriptedSession())
        self.assertIn("auth youtube", str(caught.exception))

    def test_refresh_stores_the_access_token_and_not_the_secret(self):
        save_settings(
            Settings(youtube_client_id="client", youtube_refresh_token="refresh-token", youtube_channel_title="Canal")
        )
        os.environ["REELPOST_YOUTUBE_CLIENT_SECRET"] = "secret-value"
        session = ScriptedSession()
        session.push({"access_token": "new-access", "expires_in": 3600})
        token = ensure_youtube_token(load_settings(), session)
        self.assertEqual(token, "new-access")
        saved = Path(self.tmp.name, "config.json").read_text(encoding="utf-8")
        self.assertIn("new-access", saved)
        self.assertNotIn("secret-value", saved)
        self.assertEqual(session.calls[0]["data"]["client_secret"], "secret-value")
        self.assertEqual(session.calls[0]["data"]["refresh_token"], "refresh-token")
