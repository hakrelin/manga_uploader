"""再漫画发布器本地模拟测试（不联网）。"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from manga_uploader.config import CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import zaimanhua as zmh_mod
from manga_uploader.publishers.zaimanhua import ZaimanhuaPublisher


class _Handler(BaseHTTPRequestHandler):
    requests_log: list = []

    def log_message(self, *args):  # 静默
        pass

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/userinfo"):
            self._send_json(
                {
                    "errno": 0,
                    "errmsg": "",
                    "data": {"userInfo": {"uid": 88, "nickname": "授权搬运君"}},
                }
            )
        else:
            self._send_json({"errno": -404, "errmsg": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.requests_log.append({"path": self.path, "body": body})
        if self.path.startswith("/img"):
            self._send_json(
                {"errno": 0, "errmsg": "", "data": {"file": "https://mock.cdn/page.png"}}
            )
        elif self.path.startswith("/submit"):
            self._send_json({"errno": 0, "errmsg": "", "data": {}})
        else:
            self._send_json({"errno": -400, "errmsg": "bad"}, 400)


def _make_chapter(tmp: Path, count: int = 3) -> Chapter:
    folder = tmp / "ch01"
    folder.mkdir(parents=True, exist_ok=True)
    pages = []
    for i in range(1, count + 1):
        page = folder / f"{i:03d}.png"
        Image.new("RGB", (300, 400), (i * 40 % 255, 100, 150)).save(page)
        pages.append(page)
    return Chapter(
        key="ch01",
        title="测试漫画 第01话",
        description="测试简介",
        tags=["原创"],
        pages=pages,
        source_dir=folder,
        raw={
            "title": "测试漫画",
            "platforms": {"zaimanhua": {"cate": "3"}},
        },
    )


class TestZaimanhuaPublisherMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        base = f"http://127.0.0.1:{cls.port}"
        cls._orig = {
            "user": zmh_mod.USER_INFO_URL,
            "img": zmh_mod.UPLOAD_IMG_URL,
            "submit": zmh_mod.SUBMIT_CHAPTER_URL,
        }
        zmh_mod.USER_INFO_URL = base + "/userinfo"
        zmh_mod.UPLOAD_IMG_URL = base + "/img"
        zmh_mod.SUBMIT_CHAPTER_URL = base + "/submit"

    @classmethod
    def tearDownClass(cls):
        zmh_mod.USER_INFO_URL = cls._orig["user"]
        zmh_mod.UPLOAD_IMG_URL = cls._orig["img"]
        zmh_mod.SUBMIT_CHAPTER_URL = cls._orig["submit"]
        cls.server.shutdown()

    def setUp(self):
        _Handler.requests_log = []
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _publisher(self, cookies=None):
        cfg = PlatformConfig(
            name="zaimanhua",
            cookies=cookies or {"token": "jwt-token", "clientId": "c-1"},
            settings={"cate": "3"},
        )
        common = CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        return ZaimanhuaPublisher(cfg, common)

    def test_check(self):
        result = self._publisher().check()
        self.assertTrue(result.ok)
        self.assertIn("授权搬运君", result.message)

    def test_publish_submits_chapter(self):
        chapter = _make_chapter(Path(self.tmp.name))
        result = self._publisher().publish(chapter)
        self.assertEqual(result.status, "ok", result.message)
        self.assertIn("审核", result.message)
        imgs = [r for r in _Handler.requests_log if r["path"].startswith("/img")]
        submits = [r for r in _Handler.requests_log if r["path"].startswith("/submit")]
        self.assertEqual(len(imgs), 3)
        self.assertEqual(len(submits), 1)
        body = json.loads(submits[0]["body"])
        self.assertEqual(body["name"], "测试漫画")
        self.assertEqual(body["chapter"], "短篇")
        self.assertEqual(body["cate"], "3")
        self.assertEqual(len(body["pageUrls"]), 3)
        self.assertTrue(all(u.startswith("https://mock.cdn") for u in body["pageUrls"]))

    def test_missing_token_fails(self):
        cfg = PlatformConfig(name="zaimanhua", cookies={})
        publisher = ZaimanhuaPublisher(cfg, CommonConfig())
        chapter = _make_chapter(Path(self.tmp.name))
        with self.assertRaises(Exception):
            publisher.publish(chapter)

    def test_bad_cate_rejected(self):
        cfg = PlatformConfig(name="zaimanhua", cookies={"token": "t"}, settings={"cate": "9"})
        publisher = ZaimanhuaPublisher(cfg, CommonConfig())
        chapter = _make_chapter(Path(self.tmp.name))
        chapter.raw = {"title": "测试漫画"}  # 不带平台 cate 覆盖，走配置里的 9
        with self.assertRaises(Exception):
            publisher.publish(chapter)


if __name__ == "__main__":
    unittest.main()
