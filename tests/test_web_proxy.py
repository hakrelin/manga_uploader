"""Web 端代理相关接口测试：/api/proxy/detect、/api/ip（出口 IP 检测）。"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from manga_uploader import http_client, web


class ExitIpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.yaml"
        self.cfg.write_text(
            "common: {timeout: 30}\n"
            "platforms:\n"
            "  ehentai:\n"
            "    enabled: true\n"
            "    cookies: {ipb_member_id: '1', ipb_pass_hash: h}\n"
            "    settings: {use_system_proxy: false}\n",
            encoding="utf-8",
        )
        self.state = web.ServerState(config_path=str(self.cfg))
        self.server = web.MangaServer(("127.0.0.1", 0), self.state)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_reports_blocked_region(self):
        with mock.patch.object(
            http_client, "lookup_exit_ip", return_value=("1.2.3.4", "HK")
        ):
            data = self._get("/api/ip?platform=ehentai")
        self.assertTrue(data["ok"])
        self.assertEqual(data["country"], "HK")
        self.assertEqual(data["note"], "香港")
        self.assertTrue(data["blocked"])

    def test_reports_ok_region(self):
        with mock.patch.object(
            http_client, "lookup_exit_ip", return_value=("5.6.7.8", "US")
        ):
            data = self._get("/api/ip?platform=ehentai")
        self.assertFalse(data["blocked"])
        self.assertEqual(data["ip"], "5.6.7.8")

    def test_lookup_failure_is_reported_not_crashed(self):
        with mock.patch.object(
            http_client, "lookup_exit_ip", side_effect=RuntimeError("代理不通")
        ):
            data = self._get("/api/ip?platform=ehentai")
        self.assertFalse(data["ok"])
        self.assertIn("代理不通", data["error"])

    def test_unknown_platform(self):
        # 未知平台走 400（前端按 error 提示），不要 500
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/ip?platform=nope")
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
