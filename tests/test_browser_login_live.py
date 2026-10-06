"""需要真实浏览器的端到端测试（默认跳过）。

跑法：MANGA_UPLOADER_LIVE_BROWSER=1 python -m unittest tests.test_browser_login_live

它起一个本地 HTTP 服务，让浏览器访问后下发一条 `BDUSS=...` Cookie，
再走完整流程（启动浏览器 → CDP 读 Cookie → 匹配平台规则）验证能拿到。
不依赖任何真实账号，所以可以放心在 CI/本地跑。
"""

import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from manga_uploader import browser_login as bl

LIVE = os.environ.get("MANGA_UPLOADER_LIVE_BROWSER", "").strip() not in ("", "0", "false")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><body>live test</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "BDUSS=live-test-cookie; Path=/")
        self.send_header("Set-Cookie", "STOKEN=live-stoken; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(LIVE, "设置 MANGA_UPLOADER_LIVE_BROWSER=1 才跑（需要本机有 Chrome/Edge）")
class TestLiveBrowserLogin(unittest.TestCase):
    def test_full_flow_grabs_cookie(self):
        if not bl.find_browser():
            self.skipTest("本机没有 Chrome/Edge")

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = Path(tempfile.mkdtemp(prefix="mu-live-"))
        session = None
        try:
            session = bl.BrowserLoginSession(
                "tieba", profile_dir=tmp / "profile", headless=True, timeout=45, poll=1.0
            )
            session.spec = bl.LoginSpec(
                key="live",
                label="本地测试站",
                url=f"http://127.0.0.1:{port}/",
                domains=("127.0.0.1",),
                save=("BDUSS", "STOKEN"),
                require=("BDUSS",),
            )
            cookies = session.wait_for_login(timeout=45)
            self.assertEqual(cookies.get("BDUSS"), "live-test-cookie")
            self.assertEqual(cookies.get("STOKEN"), "live-stoken")
        finally:
            if session is not None:
                session.close(kill=True)
            server.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
