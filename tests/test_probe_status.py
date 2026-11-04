"""可达性检测的状态判断。用本机的假服务端模拟握手被重置，不需要真实节点。

    python3 tests/test_probe_status.py
"""
import importlib.machinery
import importlib.util
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "sbm")
loader = importlib.machinery.SourceFileLoader("sbm", path)
spec = importlib.util.spec_from_loader("sbm", loader)
sbm = importlib.util.module_from_spec(spec)
sys.modules["sbm"] = sbm
loader.exec_module(sbm)


def local(tcp=10, tls=None, reset=False, dns="ok"):
    return {"dns": dns, "tcp_ms": tcp, "tls_ms": tls, "tls_reset": reset}


FAIL = (None, "代理请求失败")


class Classify(unittest.TestCase):
    def test_reset_with_outside_fail_is_not_down(self):
        self.assertEqual(sbm._classify(local(reset=True), FAIL, "fail"), "tls_reset")

    def test_reset_without_outside_node(self):
        self.assertEqual(sbm._classify(local(reset=True), FAIL, "n/a"), "tls_reset")

    def test_reset_but_reachable_from_outside_is_blocked(self):
        self.assertEqual(sbm._classify(local(reset=True), FAIL, "ok"), "blocked")

    def test_tcp_failure_keeps_old_results(self):
        self.assertEqual(sbm._classify(local(tcp=None), FAIL, "fail"), "down")
        self.assertEqual(sbm._classify(local(tcp=None), FAIL, "ok"), "blocked")
        self.assertEqual(sbm._classify(local(tcp=None), FAIL, "n/a"), "unreachable")

    def test_other_tls_failure_keeps_old_results(self):
        self.assertEqual(sbm._classify(local(), FAIL, "fail"), "down")

    def test_working_proxy_wins(self):
        self.assertEqual(sbm._classify(local(reset=True), (100, ""), ""), "ok")

    def test_tls_ok_proxy_fail(self):
        self.assertEqual(sbm._classify(local(tls=50), FAIL, ""), "proxy_fail")

    def test_every_status_has_a_label(self):
        for status in ("ok", "dns", "blocked", "down", "unreachable", "proxy_fail", "tls_reset"):
            self.assertIn(status, sbm.PROBE_STATUS)


class FakeServer:
    """收到 ClientHello 后结束连接：reset=True 发 RST，否则正常关闭。"""

    def __init__(self, reset):
        self.reset = reset
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(3)
            try:
                if conn.recv(4096) and self.reset:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            conn.close()


def fake_node(port):
    return {"name": "n1", "host": "127.0.0.1", "server": "n1.example.com", "sni": "n1.example.com", "port": port, "kernel": "prism"}


class LocalChecks(unittest.TestCase):
    def check(self, reset):
        srv = FakeServer(reset)
        self.addCleanup(srv.sock.close)
        with mock.patch.object(sbm, "dns_a", return_value=["127.0.0.1"]):
            return sbm._local_checks(fake_node(srv.port))

    def test_reset_is_detected(self):
        res = self.check(True)
        self.assertIsNotNone(res["tcp_ms"])
        self.assertIsNone(res["tls_ms"])
        self.assertTrue(res["tls_reset"])

    def test_plain_close_is_not_a_reset(self):
        res = self.check(False)
        self.assertIsNone(res["tls_ms"])
        self.assertFalse(res["tls_reset"])

    def test_refused_connection_is_not_a_reset(self):
        s = socket.create_server(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with mock.patch.object(sbm, "dns_a", return_value=["127.0.0.1"]):
            res = sbm._local_checks(fake_node(port))
        self.assertIsNone(res["tcp_ms"])
        self.assertFalse(res["tls_reset"])


def run(code, out=""):
    return subprocess.CompletedProcess([], code, out, "")


class ServiceNote(unittest.TestCase):
    def note(self, result):
        with mock.patch.object(sbm, "ssh", return_value=result) as ssh:
            text = sbm._service_note(fake_node(443))
        self.assertIn("prism-node-server", ssh.call_args[0][1])
        return text

    def test_active(self):
        self.assertIn("在运行", self.note(run(0, "active\n")))

    def test_stopped(self):
        self.assertIn("inactive", self.note(run(3, "inactive\n")))

    def test_ssh_failure(self):
        self.assertIn("SSH 连不上", self.note(run(255)))


class ProbeRun(unittest.TestCase):
    """完整走一遍：入库、标签和详情都用新状态。"""

    def test_record_and_summary(self):
        srv = FakeServer(True)
        self.addCleanup(srv.sock.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        node = fake_node(srv.port)
        conf = {"nodes": [node]}
        with mock.patch.multiple(sbm, PROBE_DB=f"{tmp.name}/probe.db", dns_a=mock.Mock(return_value=["127.0.0.1"]),
                                 probe_credential=mock.Mock(return_value={"password": "x"}),
                                 ensure_singbox_bin=mock.Mock(return_value="/bin/true"),
                                 _proxy_checks=mock.Mock(return_value={"n1": (None, "代理请求失败")}),
                                 _outside_check=mock.Mock(return_value=("fail", "n2")),
                                 ssh=mock.Mock(return_value=run(0, "active\n")),
                                 notify_all=mock.Mock()):
            res = sbm.probe_run(conf, log=lambda *_: None)["n1"]
            self.assertEqual(res["status"], "tls_reset")
            self.assertEqual(res["label"], sbm.PROBE_STATUS["tls_reset"])
            self.assertTrue(any("在运行" in d for d in res["detail"]))
            summary = sbm.probe_summary(conf)["nodes"]["n1"]
            self.assertEqual(summary["status"], "tls_reset")
            self.assertEqual(sbm.probe_history("n1")[-1]["label"], sbm.PROBE_STATUS["tls_reset"])


if __name__ == "__main__":
    unittest.main()
