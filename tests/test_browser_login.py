"""浏览器登录取 Cookie 的纯逻辑测试（不联网、不起浏览器）。"""

import base64
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from manga_uploader import browser_login as bl
from manga_uploader import cdp


def _jwt(exp_offset_days: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_offset_days * 86400)}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _cookie(name: str, value: str, domain: str) -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/"}


class _FakeSocket:
    """按顺序吐出预置字节，用来喂 WebSocket 解析器。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent = b""

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _timeout) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass


def _server_frame(payload: bytes, *, opcode: int = 0x1, final: bool = True) -> bytes:
    first = (0x80 if final else 0) | opcode
    size = len(payload)
    if size < 126:
        return bytes([first, size]) + payload
    if size < 65536:
        return bytes([first, 126]) + struct.pack(">H", size) + payload
    return bytes([first, 127]) + struct.pack(">Q", size) + payload


class TestWebSocketFrames(unittest.TestCase):
    def _client(self, chunks: list[bytes]) -> cdp.WebSocketClient:
        client = cdp.WebSocketClient("ws://127.0.0.1:1/x")
        client.sock = _FakeSocket(chunks)
        return client

    def test_recv_text_simple(self):
        client = self._client([_server_frame(b'{"id":1}')])
        self.assertEqual(client.recv_text(), '{"id":1}')

    def test_recv_text_large_payload(self):
        text = "x" * 70000
        client = self._client([_server_frame(text.encode())])
        self.assertEqual(client.recv_text(), text)

    def test_recv_handles_ping_then_text(self):
        client = self._client([_server_frame(b"ping", opcode=0x9), _server_frame(b"ok")])
        self.assertEqual(client.recv_text(), "ok")
        # ping 要回 pong（0x8A = FIN + pong），且带掩码
        self.assertEqual(client.sock.sent[0], 0x8A)

    def test_recv_reassembles_fragments(self):
        client = self._client(
            [
                _server_frame(b"part1-", final=False, opcode=0x1),
                _server_frame(b"part2", final=True, opcode=0x0),
            ]
        )
        self.assertEqual(client.recv_text(), "part1-part2")

    def test_recv_split_across_tcp_chunks(self):
        frame = _server_frame(b"hello")
        client = self._client([frame[:2], frame[2:4], frame[4:]])
        self.assertEqual(client.recv_text(), "hello")

    def test_send_frame_masks_payload(self):
        client = self._client([])
        client.send_text("hi")
        sent = client.sock.sent
        self.assertEqual(sent[0], 0x81)  # FIN + text
        self.assertTrue(sent[1] & 0x80)  # 客户端必须掩码
        self.assertEqual(sent[1] & 0x7F, 2)
        mask, body = sent[2:6], sent[6:]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(body)), b"hi")

    def test_close_frame_raises(self):
        client = self._client([_server_frame(b"", opcode=0x8)])
        with self.assertRaises(cdp.CDPError):
            client.recv_text()


class TestCookiePicking(unittest.TestCase):
    def test_picks_only_wanted_names_and_domains(self):
        spec = bl.spec_for("bilibili")
        rows = [
            _cookie("SESSDATA", "s", ".bilibili.com"),
            _cookie("bili_jct", "j", ".bilibili.com"),
            _cookie("DedeUserID", "1", ".bilibili.com"),
            _cookie("_uuid", "noise", ".bilibili.com"),  # 不在 save 列表里
            _cookie("SESSDATA", "other", ".example.com"),  # 域名不符
        ]
        picked = bl.pick_cookies(rows, spec)
        self.assertEqual(sorted(picked), ["DedeUserID", "SESSDATA", "bili_jct"])
        self.assertEqual(picked["SESSDATA"], "s")

    def test_zaimanhua_keeps_token_and_client_id(self):
        spec = bl.spec_for("zaimanhua")
        rows = [
            _cookie("token", "jwt-value", "manhua.zaimanhua.com"),
            _cookie("clientId", "abc", ".zaimanhua.com"),
            _cookie("token", "other-site", ".qq.com"),
        ]
        picked = bl.pick_cookies(rows, spec)
        self.assertEqual(picked, {"token": "jwt-value", "clientId": "abc"})

    def test_xiaoheihe_saves_full_cookie_string(self):
        spec = bl.spec_for("xiaoheihe")
        rows = [
            _cookie("heybox_id", "42", ".xiaoheihe.cn"),
            _cookie("pkey", "secret", ".xiaoheihe.cn"),
            _cookie("other", "noise", ".example.com"),
        ]
        picked = bl.pick_cookies(rows, spec)
        self.assertEqual(picked["heybox_id"], "42")
        self.assertIn("pkey=secret", picked["cookie"])
        self.assertNotIn("other=noise", picked["cookie"])

    def test_empty_values_are_skipped(self):
        spec = bl.spec_for("tieba")
        rows = [_cookie("BDUSS", "", ".baidu.com"), _cookie("STOKEN", "s", ".baidu.com")]
        picked = bl.pick_cookies(rows, spec)
        self.assertNotIn("BDUSS", picked)
        self.assertEqual(picked["STOKEN"], "s")


class TestReadyChecks(unittest.TestCase):
    def test_unknown_platform_rejected(self):
        with self.assertRaises(bl.BrowserLoginError):
            bl.spec_for("nope")
        self.assertFalse(bl.supports("nope"))
        self.assertTrue(bl.supports("zaimanhua"))

    def test_missing_required_cookie_keeps_waiting(self):
        spec = bl.spec_for("tieba")
        ok, message = bl.spec_ready(spec, {})
        self.assertFalse(ok)
        self.assertIn("BDUSS", message)

    def test_zaimanhua_expired_token_keeps_waiting(self):
        spec = bl.spec_for("zaimanhua")
        ok, message = bl.spec_ready(spec, {"token": _jwt(-3)})
        self.assertFalse(ok)
        self.assertIn("过期", message)

    def test_zaimanhua_fresh_token_is_ready(self):
        spec = bl.spec_for("zaimanhua")
        ok, message = bl.spec_ready(spec, {"token": _jwt(20)})
        self.assertTrue(ok)
        self.assertIn("有效期至", message)

    def test_plain_cookie_platform_ready(self):
        spec = bl.spec_for("bilibili")
        ok, _ = bl.spec_ready(spec, {"SESSDATA": "s", "bili_jct": "j"})
        self.assertTrue(ok)


class TestLauncher(unittest.TestCase):
    def test_chromium_args_include_debug_port_and_url(self):
        args = bl._chromium_args(
            r"C:\edge.exe", "https://example.com/", Path("C:/tmp/profile"), 9333, headless=False
        )
        self.assertIn(r"--user-data-dir=C:\tmp\profile", args)
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertEqual(args[-1], "https://example.com/")
        self.assertNotIn("--headless=new", args)

    def test_chromium_args_headless(self):
        args = bl._chromium_args("chrome", "https://e.com/", Path("p"), 1, headless=True)
        self.assertIn("--headless=new", args)

    def test_default_profile_dir_is_not_output_dir(self):
        path = bl.default_profile_dir()
        self.assertIn("manga_uploader", str(path).replace("\\", "/"))
        self.assertNotIn("output", str(path).replace("\\", "/").split("manga_uploader")[0])

    def test_port_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = bl.BrowserLoginSession("zaimanhua", profile_dir=Path(tmp), timeout=1)
            self.assertEqual(session._read_saved_port(), 0)
            session._port_file().write_text("12345", encoding="utf-8")
            self.assertEqual(session._read_saved_port(), 12345)
            session._port_file().write_text("garbage", encoding="utf-8")
            self.assertEqual(session._read_saved_port(), 0)


class TestWaitForLogin(unittest.TestCase):
    """用假的 CDP 数据驱动等待循环（不真的开浏览器）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = bl.BrowserLoginSession(
            "zaimanhua", profile_dir=Path(self.tmp.name), timeout=2, poll=0.05
        )
        self.session.port = 1234  # 假装已经启动
        self.original_alive = bl.debug_port_alive
        bl.debug_port_alive = lambda *_a, **_kw: True  # type: ignore[assignment]

    def tearDown(self):
        bl.debug_port_alive = self.original_alive  # type: ignore[assignment]
        self.tmp.cleanup()

    def test_waits_until_fresh_token(self):
        rows = [_cookie("token", _jwt(-1), ".zaimanhua.com")]
        self.session._cookies = lambda: list(rows)  # type: ignore[assignment]
        statuses: list[str] = []

        def flip():
            # 第二轮开始前模拟“用户在浏览器里登录完成”
            if statuses:
                rows[0] = _cookie("token", _jwt(30), ".zaimanhua.com")

        original_status = bl.spec_ready

        def wrapped(spec, picked):
            result = original_status(spec, picked)
            flip()
            return result

        bl.spec_ready = wrapped  # type: ignore[assignment]
        try:
            cookies = self.session.wait_for_login(on_status=statuses.append, timeout=5)
        finally:
            bl.spec_ready = original_status  # type: ignore[assignment]
        self.assertTrue(cookies["token"].startswith("eyJ"))
        self.assertTrue(cookies["token"])
        self.assertTrue(statuses, "应该汇报过等待状态")

    def test_browser_closed_is_reported(self):
        def boom():
            raise cdp.CDPError("boom")

        self.session._cookies = boom  # type: ignore[assignment]
        bl.debug_port_alive = lambda *_a, **_kw: False  # type: ignore[assignment]
        with self.assertRaises(bl.BrowserLoginClosed):
            self.session.wait_for_login(timeout=3)

    def test_timeout_when_nothing_arrives(self):
        self.session._cookies = lambda: []  # type: ignore[assignment]
        with self.assertRaises(bl.BrowserLoginTimeout):
            self.session.wait_for_login(timeout=0.3)

    def test_cancel(self):
        self.session._cookies = lambda: []  # type: ignore[assignment]
        with self.assertRaises(bl.BrowserLoginCancelled):
            self.session.wait_for_login(should_stop=lambda: True, timeout=5)


class TestFindBrowser(unittest.TestCase):
    def test_find_browser_does_not_crash(self):
        path = bl.find_browser()
        self.assertTrue(path is None or isinstance(path, str))

    def test_free_port_is_bindable(self):
        port = bl._free_port()
        self.assertGreater(port, 0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))  # 说明端口确实空着


if __name__ == "__main__":
    unittest.main()
