"""Web 端「浏览器登录」接口测试（把抓 Cookie 的那步替换成假函数，不真开浏览器）。"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from manga_uploader import browser_login, web


class TestBrowserLoginApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.yaml"
        self.cfg.write_text(
            "common:\n"
            "  output_dir: output\n"
            "platforms:\n"
            "  zaimanhua:\n"
            "    enabled: true\n"
            "    cookies:\n"
            "      token: old-expired-token\n"
            "    settings: {}\n",
            encoding="utf-8",
        )
        self.original = browser_login.grab_cookies
        self.state = web.ServerState(config_path=str(self.cfg))
        self.server = web.MangaServer(("127.0.0.1", 0), self.state)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        browser_login.grab_cookies = self.original  # type: ignore[assignment]
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- 工具 --

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.server.csrf_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _wait_status(self, want: tuple[str, ...], timeout: float = 8.0) -> dict:
        deadline = time.time() + timeout
        state = {}
        while time.time() < deadline:
            state = self._get("/api/browser-login/status")["state"]
            if state.get("status") in want:
                return state
            time.sleep(0.1)
        self.fail(f"等不到状态 {want}，最后是 {state}")

    # -- 用例 --

    def test_cards_flag_browser_login(self):
        cards = {c["key"]: c for c in self._get("/api/state")["cards"]}
        for key in ("bilibili", "tieba", "ehentai", "zaimanhua", "xiaoheihe"):
            self.assertTrue(cards[key].get("browser_login"), key)
            self.assertTrue(cards[key].get("browser_login_url"), key)

    def test_start_saves_cookies_into_config(self):
        def fake(platform, **_kwargs):
            self.assertEqual(platform, "zaimanhua")
            return {"token": "fresh-token", "clientId": "c-1"}

        browser_login.grab_cookies = fake  # type: ignore[assignment]
        started = self._post("/api/browser-login/start", {"platform": "zaimanhua"})
        self.assertTrue(started["ok"])
        self.assertIn("zaimanhua", started["url"])
        state = self._wait_status(("ok",))
        self.assertTrue(state["saved"])
        self.assertEqual(state["cookies"]["token"], "fresh-token")
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("fresh-token", text)
        self.assertNotIn("old-expired-token", text)
        # 其它平台不会被动到
        self.assertIn("platforms:", text)

    def test_start_rejects_unsupported_platform(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/browser-login/start", {"platform": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_worker_error_is_exposed(self):
        def boom(platform, **_kwargs):
            raise browser_login.BrowserLoginError("没找到 Chrome / Edge 浏览器")

        browser_login.grab_cookies = boom  # type: ignore[assignment]
        self._post("/api/browser-login/start", {"platform": "zaimanhua"})
        state = self._wait_status(("error",))
        self.assertIn("没找到", state["message"])

    def test_stop_cancels_waiting(self):
        def wait_forever(platform, *, should_stop=None, **_kwargs):
            for _ in range(200):
                if should_stop and should_stop():
                    raise browser_login.BrowserLoginCancelled("已取消浏览器登录")
                time.sleep(0.05)
            raise AssertionError("没有被取消")

        browser_login.grab_cookies = wait_forever  # type: ignore[assignment]
        self._post("/api/browser-login/start", {"platform": "tieba"})
        self._wait_status(("waiting", "starting"))
        self.assertTrue(self._post("/api/browser-login/stop", {})["ok"])
        state = self._wait_status(("cancelled",))
        self.assertIn("取消", state["message"])

    def test_second_start_is_rejected_while_running(self):
        def slow(platform, *, on_status=None, **_kwargs):
            if on_status:
                on_status("等待登录…")
            time.sleep(1.0)
            return {"BDUSS": "x"}

        browser_login.grab_cookies = slow  # type: ignore[assignment]
        self._post("/api/browser-login/start", {"platform": "tieba"})
        second = self._post("/api/browser-login/start", {"platform": "zaimanhua"})
        self.assertFalse(second["ok"])
        self.assertIn("已经有一个", second["error"])


if __name__ == "__main__":
    unittest.main()
