import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from mp4util import write_movie
from reelpost.cli import main
from reelpost.config import save_settings
from reelpost.config import Settings


TOKEN_ENV = (
    "REELPOST_APP_ID",
    "REELPOST_APP_SECRET",
    "REELPOST_CONFIG_ID",
    "REELPOST_USER_TOKEN",
    "REELPOST_PAGE_ID",
    "REELPOST_PAGE_NAME",
    "REELPOST_PAGE_TOKEN",
    "REELPOST_IG_USER_ID",
    "REELPOST_IG_USERNAME",
    "REELPOST_GRAPH_VERSION",
    "REELPOST_YOUTUBE_CLIENT_ID",
    "REELPOST_YOUTUBE_CLIENT_SECRET",
    "REELPOST_YOUTUBE_REFRESH_TOKEN",
    "REELPOST_YOUTUBE_ACCESS_TOKEN",
    "REELPOST_YOUTUBE_CHANNEL_ID",
    "REELPOST_YOUTUBE_CHANNEL_TITLE",
)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = {key: os.environ.get(key) for key in ("REELPOST_CONFIG_DIR", *TOKEN_ENV)}
        for key in TOKEN_ENV:
            os.environ.pop(key, None)
        os.environ["REELPOST_CONFIG_DIR"] = self.tmp.name
        self.video = write_movie(Path(self.tmp.name) / "clip.mp4", timescale=1000, duration=27000)
        self.cover = Path(self.tmp.name) / "cover.jpg"
        Image.new("RGB", (90, 160), "red").save(self.cover, "JPEG")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_check_accepts_a_short_vertical_video(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["check", str(self.video), "--description", "Hello", "--tags", "berlin"])
        self.assertEqual(code, 0)
        self.assertIn("Ready.", stdout.getvalue())
        self.assertIn("#berlin", stdout.getvalue())

    def test_long_video_is_rejected_before_upload(self):
        long = write_movie(Path(self.tmp.name) / "long.mp4", timescale=1000, duration=100000)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["publish", str(long), "--to", "facebook", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("90 seconds", stderr.getvalue())
        self.assertIn("Nothing was published", stderr.getvalue())

    def test_publish_requires_yes_when_stdin_is_not_a_terminal(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["publish", str(self.video), "--description", "Hello"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", stderr.getvalue())

    def test_dry_run_prints_the_plan_and_hides_the_token(self):
        save_settings(
            Settings(
                page_id="10",
                page_name="Brand",
                page_access_token="SECRET-TOKEN-VALUE",
                ig_user_id="20",
                ig_username="brand",
            )
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "publish",
                    str(self.video),
                    "--description", "Morning",
                    "--tags", "berlin,travel",
                    "--thumbnail", str(self.cover),
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["targets"], ["facebook", "instagram", "youtube"])
        self.assertEqual(payload["title"], "Morning")
        self.assertIn("#Shorts", payload["youtube_description"])
        self.assertNotIn("#Shorts", payload["caption"])
        self.assertIn("#berlin", payload["caption"])
        self.assertIn("#travel", payload["caption"])
        self.assertEqual(payload["page_name"], "Brand")
        self.assertNotIn("SECRET-TOKEN-VALUE", stdout.getvalue())
        self.assertNotIn("SECRET-TOKEN-VALUE", stderr.getvalue())

    def test_publish_sends_the_same_caption_to_both(self):
        save_settings(
            Settings(
                page_id="10",
                page_name="Brand",
                page_access_token="page-token",
                ig_user_id="20",
                ig_username="brand",
            )
        )
        calls = {}

        def fake_facebook(client, page_id, video, description, **kwargs):
            calls["facebook"] = {
                "page_id": page_id,
                "description": description,
                "thumbnail": kwargs["thumbnail"],
                "title": kwargs["title"],
            }
            return {"video_id": "99", "permalink": "https://www.facebook.com/reel/99", "thumbnail": True}

        def fake_instagram(client, ig_user_id, video, caption, **kwargs):
            calls["instagram"] = {"ig_user_id": ig_user_id, "caption": caption, "cover_url": kwargs["cover_url"]}
            return {"media_id": "55", "permalink": "https://www.instagram.com/reel/abc/", "confirmed": True}

        def fake_youtube(_session, _token, _video, caption, **kwargs):
            calls["youtube"] = {
                "caption": caption,
                "title": kwargs["title"],
                "tags": kwargs["tags"],
                "thumbnail": kwargs["thumbnail"],
            }
            return {
                "video_id": "abcdefghijk",
                "url": "https://www.youtube.com/shorts/abcdefghijk",
                "thumbnail": True,
                "privacy": "public",
                "scheduled": False,
            }

        @contextlib.contextmanager
        def fake_cover(_path, timeout=45):
            yield "https://cover.example/cover.jpg"

        stdout = io.StringIO()
        with (
            mock.patch("reelpost.cli.publish_facebook", fake_facebook),
            mock.patch("reelpost.cli.publish_instagram", fake_instagram),
            mock.patch("reelpost.cli.publish_youtube", fake_youtube),
            mock.patch("reelpost.cli.ensure_youtube_token", return_value="yt-token"),
            mock.patch("reelpost.cli.public_cover", fake_cover),
            mock.patch("reelpost.cli.read_quota", return_value=(1, 50)),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(
                [
                    "publish",
                    str(self.video),
                    "--description", "Morning",
                    "--tags", "berlin",
                    "--thumbnail", str(self.cover),
                    "--yes",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls["facebook"]["description"], calls["instagram"]["caption"])
        self.assertEqual(calls["facebook"]["description"], calls["youtube"]["caption"])
        self.assertNotIn("#Shorts", calls["facebook"]["description"])
        self.assertEqual(calls["youtube"]["title"], "Morning")
        self.assertEqual(calls["youtube"]["tags"], ["berlin"])
        self.assertEqual(calls["facebook"]["title"], "Morning")
        self.assertIn("#berlin", calls["facebook"]["description"])
        self.assertEqual(calls["facebook"]["page_id"], "10")
        self.assertEqual(calls["instagram"]["ig_user_id"], "20")
        self.assertEqual(calls["instagram"]["cover_url"], "https://cover.example/cover.jpg")
        self.assertTrue(str(calls["facebook"]["thumbnail"]).endswith("cover.jpg"))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["facebook"]["video_id"], "99")
        self.assertEqual(payload["instagram"]["media_id"], "55")
        self.assertEqual(payload["youtube"]["video_id"], "abcdefghijk")

    def test_status_does_not_print_the_token(self):
        save_settings(Settings(page_id="10", page_name="Brand", page_access_token="SECRET-TOKEN-VALUE"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["auth", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET-TOKEN-VALUE", stdout.getvalue())
        self.assertIn("Brand", stdout.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["page_token"])

    def test_youtube_only_does_not_touch_meta(self):
        calls = {}

        def fake_youtube(_session, _token, _video, caption, **kwargs):
            calls["caption"] = caption
            calls["title"] = kwargs["title"]
            return {"video_id": "abcdefghijk", "url": "https://www.youtube.com/shorts/abcdefghijk", "thumbnail": False, "privacy": "public"}

        with (
            mock.patch("reelpost.cli.resolve_page") as resolve_page,
            mock.patch("reelpost.cli.ensure_youtube_token", return_value="yt-token"),
            mock.patch("reelpost.cli.publish_youtube", fake_youtube),
        ):
            code = main(
                ["publish", str(self.video), "--to", "youtube", "--description", "Morning", "--tags", "berlin", "--yes"]
            )
        self.assertEqual(code, 0)
        resolve_page.assert_not_called()
        self.assertEqual(calls["title"], "Morning")
        self.assertNotIn("#Shorts", calls["caption"])

    def test_schedule_allows_youtube_and_rejects_instagram(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            ok = main(
                ["check", str(self.video), "--to", "youtube", "--schedule", "2026-10-29T09:00+01:00", "--description", "Tomorrow"]
            )
        self.assertEqual(ok, 0)
        self.assertIn("Ready.", stdout.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            blocked = main(
                ["check", str(self.video), "--to", "instagram,youtube", "--schedule", "2026-10-29T09:00+01:00"]
            )
        self.assertEqual(blocked, 1)
        self.assertIn("Instagram cannot be scheduled", stderr.getvalue())

    def test_landscape_is_rejected_for_youtube(self):
        wide = write_movie(Path(self.tmp.name) / "wide.mp4", timescale=1000, duration=5000, width=1920, height=1080)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["check", str(wide), "--to", "youtube"])
        self.assertEqual(code, 1)
        self.assertIn("vertical", stderr.getvalue())

    def test_status_shows_a_youtube_channel_without_the_refresh_token(self):
        save_settings(
            Settings(
                youtube_refresh_token="SECRET-REFRESH",
                youtube_channel_id="UCcanalcanal",
                youtube_channel_title="Canal",
            )
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["auth", "status"])
        self.assertEqual(code, 0)
        self.assertIn("Canal", stdout.getvalue())
        self.assertNotIn("Not logged in.", stdout.getvalue())
        self.assertNotIn("SECRET-REFRESH", stdout.getvalue())
        encoded = io.StringIO()
        with contextlib.redirect_stdout(encoded):
            code = main(["auth", "status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(encoded.getvalue())
        self.assertTrue(payload["logged_in"])
        self.assertTrue(payload["youtube"]["refresh_token"])
        self.assertEqual(payload["youtube"]["channel_title"], "Canal")
        self.assertNotIn("SECRET-REFRESH", encoded.getvalue())

    def test_youtube_logout_keeps_the_page(self):
        save_settings(
            Settings(
                page_id="10",
                page_access_token="page-token",
                youtube_refresh_token="refresh-token",
                youtube_channel_title="Canal",
            )
        )
        code = main(["auth", "logout", "--youtube"])
        self.assertEqual(code, 0)
        from reelpost.config import load_settings

        settings = load_settings()
        self.assertEqual(settings.page_access_token, "page-token")
        self.assertIsNone(settings.youtube_refresh_token)
        self.assertIsNone(settings.youtube_channel_title)
