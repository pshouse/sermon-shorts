"""Guest-token lookup for the Subsplash fetch. The live feed is not called."""

import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from sermon_shorts import subsplash
except ImportError:  # httpx, an optional import of this module, is missing
    subsplash = None


def _cookie(name, path, value):
    return SimpleNamespace(name=name, path=path, value=value)


@unittest.skipIf(subsplash is None, "httpx not installed")
class GuestTokenTest(unittest.TestCase):
    def test_the_church_path_cookie_wins_over_the_app_key_cookie(self):
        cookies = [
            _cookie("ss-token-guest", "/u/-8MFVDR", "app-key-token"),
            _cookie("ss-token-guest", "/u/gastoncommunitychurch", "path-token"),
            _cookie("ss-token-global", "/u/gastoncommunitychurch", "global"),
        ]
        self.assertEqual(subsplash._guest_token(cookies, "gastoncommunitychurch"), "path-token")

    def test_any_guest_cookie_is_used_when_the_path_does_not_match(self):
        cookies = [_cookie("ss-token-guest", "/u/-8MFVDR", "app-key-token")]
        self.assertEqual(subsplash._guest_token(cookies, "gastoncommunitychurch"), "app-key-token")

    def test_an_empty_guest_cookie_is_ignored(self):
        cookies = [_cookie("ss-token-guest", "/u/gastoncommunitychurch", "")]
        self.assertIsNone(subsplash._guest_token(cookies, "gastoncommunitychurch"))

    def test_app_key_comes_from_init(self):
        self.assertEqual(subsplash._app_key({"appData": {"id": "8MFVDR"}}), "8MFVDR")
        self.assertIsNone(subsplash._app_key({"appData": {}}))
        self.assertIsNone(subsplash._app_key([]))


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.headers = {}
        self.cookies = SimpleNamespace(jar=[])
        self.closed = False
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append(url)
        if url.endswith("/api/session"):
            self.cookies.jar = [
                _cookie("ss-token-guest", "/u/-8MFVDR", "app-key-token"),
                _cookie("ss-token-guest", "/u/gastoncommunitychurch", "path-token"),
            ]
            return SimpleNamespace(status_code=200)
        if url.endswith("/api/init"):
            return SimpleNamespace(status_code=200, json=lambda: {"appData": {"id": "8MFVDR"}})
        raise AssertionError(url)

    def close(self):
        self.closed = True


@unittest.skipIf(subsplash is None, "httpx not installed")
class GuestSessionTest(unittest.TestCase):
    def test_session_and_init_become_a_bearer_and_an_app_key(self):
        client = _FakeClient()
        with mock.patch.object(subsplash.httpx, "Client", return_value=client):
            returned, app_key = subsplash._guest_session("gastoncommunitychurch")
        self.assertIs(returned, client)
        self.assertEqual(app_key, "8MFVDR")
        self.assertEqual(client.headers["Authorization"], "Bearer path-token")
        self.assertEqual(client.headers["Accept"], "application/json")
        self.assertEqual(client.calls, [
            "https://subsplash.com/u/gastoncommunitychurch/api/session",
            "https://subsplash.com/u/gastoncommunitychurch/api/init",
        ])
        self.assertFalse(client.closed)

    def test_a_session_without_a_guest_cookie_closes_the_client(self):
        client = _FakeClient()
        client.get = lambda url, headers=None: SimpleNamespace(status_code=200, json=lambda: {})
        with mock.patch.object(subsplash.httpx, "Client", return_value=client):
            with self.assertRaises(subsplash.SubsplashError) as raised:
                subsplash._guest_session("gastoncommunitychurch")
        self.assertIn("guest token", str(raised.exception))
        self.assertTrue(client.closed)

    def test_a_missing_app_key_is_an_error(self):
        client = _FakeClient()
        real_get = client.get

        def get(url, headers=None):
            if url.endswith("/api/init"):
                return SimpleNamespace(status_code=200, json=lambda: {"appData": {}})
            return real_get(url, headers)

        client.get = get
        with mock.patch.object(subsplash.httpx, "Client", return_value=client):
            with self.assertRaises(subsplash.SubsplashError) as raised:
                subsplash._guest_session("gastoncommunitychurch")
        self.assertIn("app key", str(raised.exception))
        self.assertTrue(client.closed)
