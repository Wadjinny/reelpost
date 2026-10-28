import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from reelpost.accounts import choose_page, public_page
from reelpost.auth import _oauth_server, build_login_url, parse_oauth_redirect
from reelpost.config import load_settings, save_settings
from reelpost.config import Settings
from reelpost.cover_host import find_trycloudflare_url, make_cover_handler, start_cover_server
from reelpost.errors import ReelpostError
from reelpost.schedule import parse_schedule


class AuthTests(unittest.TestCase):
    def test_login_url_uses_scopes_or_a_config_id(self):
        scoped = build_login_url(app_id="123", redirect_uri="http://localhost/callback", state="abc", version="v26.0")
        self.assertIn("scope=pages_show_list", scoped)
        self.assertIn("state=abc", scoped)
        configured = build_login_url(
            app_id="123",
            redirect_uri="http://localhost/callback",
            state="abc",
            version="v26.0",
            config_id="cfg",
        )
        self.assertIn("config_id=cfg", configured)
        self.assertNotIn("scope=", configured)

    def test_redirect_requires_the_matching_state(self):
        code = parse_oauth_redirect("http://localhost/callback?code=abc123&state=xyz", "xyz")
        self.assertEqual(code, "abc123")
        with self.assertRaises(ReelpostError):
            parse_oauth_redirect("http://localhost/callback?code=abc123&state=nope", "xyz")

    def test_local_callback_records_the_code_and_hides_it_from_the_log(self):
        box = {}
        server = _oauth_server(box, "state-1", host="127.0.0.1", port=0)
        port = server.server_address[1]

        def serve_one():
            server.timeout = 3
            server.handle_request()
            server.server_close()

        thread = threading.Thread(target=serve_one)
        thread.start()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc123456789&state=state-1") as response:
            body = response.read()
        thread.join(3)
        self.assertEqual(box["code"], "abc123456789")
        self.assertNotIn(b"abc123456789", body)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = os.environ.get("REELPOST_CONFIG_DIR")
        self._page = os.environ.get("REELPOST_PAGE_ID")
        os.environ["REELPOST_CONFIG_DIR"] = self.tmp.name
        os.environ.pop("REELPOST_PAGE_ID", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("REELPOST_CONFIG_DIR", None)
        else:
            os.environ["REELPOST_CONFIG_DIR"] = self._saved
        if self._page is None:
            os.environ.pop("REELPOST_PAGE_ID", None)
        else:
            os.environ["REELPOST_PAGE_ID"] = self._page

    def test_config_file_is_private_and_env_wins(self):
        save_settings(Settings(page_id="from-file", page_access_token="secret-token"))
        mode = Path(self.tmp.name, "config.json").stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        os.environ["REELPOST_PAGE_ID"] = "from-env"
        settings = load_settings()
        self.assertEqual(settings.page_id, "from-env")
        self.assertEqual(settings.page_access_token, "secret-token")


class AccountTests(unittest.TestCase):
    def test_public_page_omits_the_token(self):
        shown = public_page({"id": "1", "name": "Brand", "access_token": "secret", "ig_username": "brand"})
        self.assertNotIn("access_token", shown)
        self.assertNotIn("secret", shown.values())

    def test_choose_page_by_name(self):
        pages = [
            {"id": "1", "name": "One"},
            {"id": "2", "name": "Two"},
        ]
        chosen = choose_page(pages, "two", interactive=False)
        self.assertEqual(chosen["id"], "2")


class ScheduleTests(unittest.TestCase):
    def test_offset_is_converted_to_utc(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        when = parse_schedule("2026-10-29T09:00+01:00", now=now)
        self.assertEqual(when, datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc))

    def test_rejects_a_time_inside_ten_minutes(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        with self.assertRaises(ReelpostError):
            parse_schedule("2026-10-28T12:05+00:00", now=now)


class CoverHostTests(unittest.TestCase):
    def test_finds_a_quick_tunnel_url(self):
        text = "INF  https://random-name.trycloudflare.com  "
        self.assertEqual(find_trycloudflare_url(text), "https://random-name.trycloudflare.com")

    def test_serves_only_the_cover(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cover.jpg"
            Image.new("RGB", (90, 160), "red").save(path, "JPEG")
            server = start_cover_server(path)
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/cover.jpg") as response:
                self.assertTrue(response.headers["Content-Type"].startswith("image/jpeg"))
                self.assertEqual(response.read(), path.read_bytes())
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/other.jpg")
        self.assertTrue(make_cover_handler(path))
