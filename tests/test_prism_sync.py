"""账号同步和 Prism 节点相关的逻辑。不需要真实节点：SSH 推送被替换掉。

    python3 tests/test_prism_sync.py
"""
import base64
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "sbm")
loader = importlib.machinery.SourceFileLoader("sbm", path)
spec = importlib.util.spec_from_loader("sbm", loader)
sbm = importlib.util.module_from_spec(spec)
sys.modules["sbm"] = sbm
loader.exec_module(sbm)


def node(name, kernel=None):
    n = {"name": name, "host": f"{name}.example.com", "server": f"{name}.example.com", "sni": f"{name}.example.com", "port": 443}
    if kernel:
        n["kernel"] = kernel
    return n


def user(name, cred, enabled=True):
    return {"name": name, "cred": cred, "password": f"pw-{cred}", "enabled": enabled, "id": name, "token": name}


def new_creds(retiring=()):
    return {"spares": [{"name": "spare1", "password": "pw-spare1"}], "retiring": list(retiring), "marks": {}}


class SyncPrism(unittest.TestCase):
    def setUp(self):
        self.pushed = {}
        self.failing = set()

        def push(n, cred_list, marks):
            if n["name"] in self.failing:
                return False, "ssh failed"
            self.pushed[n["name"]] = sorted(c["name"] for c in cred_list)
            return True, "APPLIED"
        for target, value in (("push_node", push), ("save_creds", lambda c: None)):
            p = mock.patch.object(sbm, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_retired_account_leaves_prism_nodes_only(self):
        users = [user("a", "c1")]
        creds = new_creds([{"name": "c2", "password": "pw-c2"}])
        conf = {"nodes": [node("p1", "prism"), node("s1")]}
        sbm.sync_prism(conf, users, creds, log=lambda *_: None)
        self.assertEqual(self.pushed, {"p1": ["c1", "spare1"]})
        # 账号的统计编号要留着：节点上还有它最后一段流量没有采集
        self.assertIn("c2", creds["marks"])
        self.assertEqual(creds["retiring"][0]["name"], "c2")

    def test_reenabled_user_keeps_the_account(self):
        # 账号还在 retiring 里但用户已经启用（user_enable 的中间状态）不能被删
        users = [user("a", "c1")]
        creds = new_creds([{"name": "c1", "password": "pw-c1"}])
        sbm.sync_prism({"nodes": [node("p1", "prism")]}, users, creds, log=lambda *_: None)
        self.assertIn("c1", self.pushed["p1"])

    def test_sing_box_only_fleet_is_untouched(self):
        creds = new_creds()
        sbm.sync_prism({"nodes": [node("s1")]}, [user("a", "c1")], creds)
        self.assertEqual(self.pushed, {})
        self.assertNotIn("dirty", creds)

    def test_failed_push_is_retried_by_maintenance(self):
        self.failing = {"p2"}
        creds = dict(new_creds(), dirty=["s1", "p1"])
        lines = []
        conf = {"nodes": [node("p1", "prism"), node("p2", "prism"), node("s1")]}
        sbm.sync_prism(conf, [user("a", "c1")], creds, log=lines.append)
        self.assertEqual(creds["dirty"], ["s1", "p2"])
        self.assertTrue(any("p2" in line for line in lines))


class SingBoxBinary(unittest.TestCase):
    def test_prism_nodes_are_not_asked_for_a_sing_box_binary(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sbm, "SB_BIN", f"{d}/sing-box"), \
                mock.patch.object(sbm, "download_singbox", return_value=None) as dl, \
                mock.patch.object(sbm.subprocess, "run") as run:
            self.assertIsNone(sbm.ensure_singbox_bin({"nodes": [node("p1", "prism")]}))
            run.assert_not_called()
            dl.assert_called_once()

    def test_download_is_throttled(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sbm, "SB_BIN", f"{d}/sing-box"), \
                mock.patch.object(sbm, "releases", side_effect=sbm.SbmError("offline")) as rel, \
                mock.patch.object(sbm.sys, "stderr"):
            self.assertIsNone(sbm.download_singbox({"nodes": []}))
            self.assertIsNone(sbm.download_singbox({"nodes": []}))
            self.assertEqual(rel.call_count, 1)


class Subscriptions(unittest.TestCase):
    def test_share_links_carry_the_fingerprint(self):
        with mock.patch.object(sbm, "profile_nodes", return_value=[node("n1")]):
            text = base64.b64decode(sbm.build_uri({}, {"password": "p/w"}, profile={})).decode()
        self.assertEqual(text, "anytls://p%2Fw@n1.example.com:443/?sni=n1.example.com&fp=chrome#n1")


if __name__ == "__main__":
    unittest.main()
