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
        self.assertEqual(payload["targets"], ["facebook", "instagram"])
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
            calls["facebook"] = {"page_id": page_id, "description": description, "thumbnail": kwargs["thumbnail"]}
            return {"video_id": "99", "permalink": "https://www.facebook.com/reel/99", "thumbnail": True}

        def fake_instagram(client, ig_user_id, video, caption, **kwargs):
            calls["instagram"] = {"ig_user_id": ig_user_id, "caption": caption, "cover_url": kwargs["cover_url"]}
            return {"media_id": "55", "permalink": "https://www.instagram.com/reel/abc/", "confirmed": True}

        @contextlib.contextmanager
        def fake_cover(_path, timeout=45):
            yield "https://cover.example/cover.jpg"

        stdout = io.StringIO()
        with (
            mock.patch("reelpost.cli.publish_facebook", fake_facebook),
            mock.patch("reelpost.cli.publish_instagram", fake_instagram),
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
        self.assertIn("#berlin", calls["facebook"]["description"])
        self.assertEqual(calls["facebook"]["page_id"], "10")
        self.assertEqual(calls["instagram"]["ig_user_id"], "20")
        self.assertEqual(calls["instagram"]["cover_url"], "https://cover.example/cover.jpg")
        self.assertTrue(str(calls["facebook"]["thumbnail"]).endswith("cover.jpg"))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["facebook"]["video_id"], "99")
        self.assertEqual(payload["instagram"]["media_id"], "55")

    def test_status_does_not_print_the_token(self):
        save_settings(Settings(page_id="10", page_name="Brand", page_access_token="SECRET-TOKEN-VALUE"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["auth", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET-TOKEN-VALUE", stdout.getvalue())
        self.assertIn("Brand", stdout.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["page_token"])
