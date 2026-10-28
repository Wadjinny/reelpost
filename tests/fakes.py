"""Scripted HTTP session for Graph API tests."""

import json


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class ScriptedSession:
    def __init__(self):
        self.queue = []
        self.calls = []
        self.headers = {}

    def push(self, payload, status=200):
        self.queue.append(FakeResponse(payload, status))
        return self

    def push_exc(self, exc):
        self.queue.append(exc)
        return self

    def _next(self):
        if not self.queue:
            raise AssertionError("unexpected request")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, headers=None, files=None, timeout=None, params=None):
        body = data.read() if hasattr(data, "read") else data
        file_bodies = {}
        if files:
            for key, value in files.items():
                handle = value[1]
                file_bodies[key] = handle.read() if hasattr(handle, "read") else handle
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "data": body,
                "form": data if files else None,
                "headers": headers,
                "files": file_bodies,
            }
        )
        return self._next()

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        return self._next()
