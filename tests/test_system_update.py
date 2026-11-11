import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("sbm_update", Path(__file__).parents[1] / "web/sbm_update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        update._cache = None
        update._attempt = 0

    def response(self, tag):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"tag_name": tag}).encode()
        return response

    def test_newer_release_and_cache(self):
        with patch.object(update.urllib.request, "urlopen", return_value=self.response("v1.2.0")) as fetch:
            self.assertTrue(update.check()["available"])
            self.assertTrue(update.check(force=True)["available"])
            self.assertEqual(fetch.call_count, 1)

    def test_equal_and_older_are_not_updates(self):
        for tag in ("v1.0.0", "v0.9.9"):
            update._cache = None
            with patch.object(update.urllib.request, "urlopen", return_value=self.response(tag)):
                self.assertFalse(update.check()["available"])

    def test_numeric_comparison(self):
        self.assertGreater(update.version_tuple("v1.10.0"), update.version_tuple("v1.9.9"))
        for tag in ("../main", "v1.0.0-beta", "v01.0.0"):
            with self.assertRaises(ValueError):
                update.version_tuple(tag)

    def test_failure_preserves_previous_release(self):
        with patch.object(update.urllib.request, "urlopen", return_value=self.response("v1.1.0")):
            update.check()
        update._attempt = 0
        with patch.object(update.urllib.request, "urlopen", side_effect=TimeoutError("timeout")):
            result = update.check()
        self.assertEqual(result["latest"], "v1.1.0")
        self.assertTrue(result["available"])
        self.assertTrue(result["error"])

    def test_bad_tag_is_not_used_as_link(self):
        with patch.object(update.urllib.request, "urlopen", return_value=self.response("../../bad")):
            result = update.check()
        self.assertTrue(result["error"])
        self.assertEqual(result["url"], update.REPO + "/releases")


if __name__ == "__main__":
    unittest.main()
