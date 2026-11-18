"""Instagram's rupload 400 is not, by itself, a failed upload."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from sermon_shorts import publish
except ImportError:
    publish = None

_KEPT = ("400", {
    "debug_info": {
        "retriable": False,
        "type": "ProcessingFailedError",
        "message": "Request processing failed",
    },
})


def _response(status, body):
    text = body if isinstance(body, str) else json.dumps(body)
    return SimpleNamespace(
        status_code=status, text=text, is_error=status >= 400,
        json=lambda: json.loads(text))


@unittest.skipIf(publish is None, "publish dependencies not installed")
class RuploadResponseTest(unittest.TestCase):
    def test_the_known_400_means_the_file_was_kept(self):
        self.assertTrue(publish._rupload_kept_the_file(_response(400, _KEPT[1])))

    def test_a_different_processing_error_is_a_real_rejection(self):
        body = {"debug_info": {"type": "ProcessingFailedError",
                               "message": "unauthorized user request"}}
        self.assertFalse(publish._rupload_kept_the_file(_response(400, body)))

    def test_a_200_is_not_this_case(self):
        self.assertFalse(publish._rupload_kept_the_file(
            _response(200, {"success": True})))


@unittest.skipIf(publish is None, "publish dependencies not installed")
class PublishInstagramTest(unittest.TestCase):
    def _clip(self):
        return {"title": "The Holy Spirit Speaks", "description": "A caption."}

    def _run(self, upload, statuses):
        calls = []

        def graph(method, path, token, **kwargs):
            calls.append((method, path))
            if method == "POST" and str(path).endswith("/media"):
                return {"id": "CONT", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/CONT"}
            if method == "GET" and path == "CONT":
                return {"status_code": statuses.pop(0), "status": "detail"}
            if method == "POST" and str(path).endswith("/media_publish"):
                return {"id": "MEDIA"}
            if method == "GET" and path == "MEDIA":
                return {"permalink": "https://www.instagram.com/reel/MEDIA/"}
            raise AssertionError((method, path))

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(publish, "_meta_token", return_value="tok"), \
                mock.patch.object(publish, "_meta_ids", return_value={"ig_user_id": "IG"}), \
                mock.patch.object(publish.httpx, "post", return_value=upload) as post, \
                mock.patch.object(publish, "_graph", side_effect=graph), \
                mock.patch.object(publish.time, "sleep"):
            mp4 = Path(tmp) / "clip.mp4"
            mp4.write_bytes(b"not a real video")
            try:
                result = publish.publish_instagram(self._clip(), mp4, None)
            except publish.PublishError as e:
                result = e
        return result, calls, post.call_count

    def test_a_400_still_publishes_once_the_container_finishes(self):
        result, calls, uploads = self._run(_response(400, _KEPT[1]), ["FINISHED"])
        self.assertEqual(result, ("MEDIA", "https://www.instagram.com/reel/MEDIA/", None))
        self.assertEqual(uploads, 1)
        self.assertIn(("POST", "IG/media_publish"), calls)

    def test_a_400_followed_by_an_error_container_is_not_published(self):
        result, calls, uploads = self._run(_response(400, _KEPT[1]), ["ERROR"])
        self.assertIsInstance(result, publish.PublishError)
        self.assertIn("couldn't process", str(result))
        self.assertEqual(uploads, 1)
        self.assertNotIn(("POST", "IG/media_publish"), calls)

    def test_any_other_upload_error_stops_before_the_container_is_polled(self):
        body = {"debug_info": {"type": "ProcessingFailedError", "message": "nope"}}
        result, calls, uploads = self._run(_response(400, body), ["FINISHED"])
        self.assertIsInstance(result, publish.PublishError)
        self.assertIn("video upload to Meta failed", str(result))
        self.assertEqual(uploads, 1)
        self.assertEqual([c for c in calls if c[0] == "GET"], [])
