"""Run on Linux: python3 -m unittest discover -s tests -p test_prism_phone.py."""
import base64
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader("phone_test_sbm", str(Path(__file__).parents[1] / "bin/sbm"))
spec = importlib.util.spec_from_loader(loader.name, loader)
sbm = importlib.util.module_from_spec(spec)
loader.exec_module(sbm)


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.conf = {"sub_domain": "sub.example.com", "sub_port": 8443, "nodes": [
            {"name": "HK-01", "server": "hk.example.com", "sni": "hk.example.com", "port": 443},
            {"name": "JP-01", "server": "jp.example.com", "sni": "jp.example.com", "port": 38101}]}
        self.user = {"name": "example", "token": "a" * 32, "password": "test-only-密钥", "enabled": True}

    def test_basic_nodes_with_certificate_validation(self):
        data = sbm.build_prism_phone(self.conf, self.user)
        self.assertEqual(set(data), {"outbounds"})
        self.assertEqual(len(data["outbounds"]), 2)
        for outbound in data["outbounds"]:
            self.assertEqual(outbound["type"], "anytls")
            self.assertEqual(outbound["password"], self.user["password"])
            self.assertEqual(set(outbound["tls"]), {"enabled", "server_name"})
            self.assertTrue(outbound["tls"]["enabled"])
        self.assertEqual(data["outbounds"][1]["server_port"], 38101)

    def test_profile_subset_and_original_fingerprint(self):
        profile = sbm.default_profile()
        profile["nodes"] = ["JP-01"]
        self.assertEqual([n["tag"] for n in sbm.build_prism_phone(self.conf, self.user, profile)["outbounds"]], ["JP-01"])
        self.assertEqual(sbm.outbound_singbox(self.conf["nodes"][0], self.user)["tls"]["utls"]["fingerprint"], "chrome")
        self.assertIn("fp=chrome", base64.b64decode(sbm.build_uri(self.conf, self.user)).decode())

    def test_links_and_preview(self):
        self.assertIn("?t=prism-phone", next(v for k, v in sbm.sub_links(self.conf, self.user).items() if "Prism Phone" in k))
        with tempfile.TemporaryDirectory() as directory, patch.object(sbm, "WWW", directory):
            sub = Path(directory) / self.user["token"]
            sub.mkdir()
            (sub / "prism-phone.json").write_text(json.dumps(sbm.build_prism_phone(self.conf, self.user)), encoding="utf-8")
            self.assertTrue(sbm.read_sub_files(self.user)["prism_phone"])
            self.user["enabled"] = False
            self.assertIsNone(sbm.read_sub_files(self.user))


if __name__ == "__main__":
    unittest.main()
