import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from reelpost.errors import GraphAPIError
from reelpost.graph import GraphClient, facebook_upload_url
from fakes import ScriptedSession


class GraphTests(unittest.TestCase):
    def test_upload_sends_the_file_with_oauth_headers(self):
        payload = b"hello reel bytes"
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received["body"] = self.rfile.read(length)
                received["authorization"] = self.headers["Authorization"]
                received["offset"] = self.headers["offset"]
                received["file_size"] = self.headers["file_size"]
                body = b'{"success": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(payload)
            client = GraphClient("TOKEN", session=requests.Session())
            client.upload(f"http://127.0.0.1:{server.server_address[1]}/up", video, offset=4)
        self.assertEqual(received["body"], payload[4:])
        self.assertEqual(received["authorization"], "OAuth TOKEN")
        self.assertEqual(received["offset"], "4")
        self.assertEqual(received["file_size"], str(len(payload)))

    def test_redacts_the_token_in_graph_errors(self):
        session = ScriptedSession()
        session.push({"error": {"message": "bad TOKEN-SECRET", "code": 190}})
        client = GraphClient("TOKEN-SECRET", session=session)
        with self.assertRaises(GraphAPIError) as caught:
            client.get("/me")
        self.assertNotIn("TOKEN-SECRET", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_versioned_facebook_upload_url(self):
        url = facebook_upload_url("99", "https://rupload.facebook.com/video-upload/99", "v26.0")
        self.assertEqual(url, "https://rupload.facebook.com/video-upload/v26.0/99")
        given = "https://rupload.facebook.com/video-upload/v26.0/99"
        self.assertEqual(facebook_upload_url("99", given, "v26.0"), given)
