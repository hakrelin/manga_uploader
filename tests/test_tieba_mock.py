import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from manga_uploader.config import CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import tieba as tieba_mod
from manga_uploader.publishers.tieba import TiebaPublisher


class _Handler(BaseHTTPRequestHandler):
    log: list = []

    def log_message(self, *args):
        pass

    def _reply_json(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("/tbs"):
            self._reply_json({"is_login": 1, "tbs": "tok123"})
        elif path.endswith("/f"):
            html = b'<html><script>window.PageData={"fid":42}</script></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self._reply_json({"no": 1, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.log.append({"path": self.path, "body": body})
        if "upload_image" in self.path:
            self._reply_json({"errno": 0, "imgurl": "http://mock.baidu.com/a.jpg"})
        elif "thread/add" in self.path:
            self._reply_json({"no": 0, "error": "", "data": {"tid": "123"}})
        elif "post/add" in self.path:
            self._reply_json({"no": 0, "error": "", "data": {"pid": "456"}})
        else:
            self._reply_json({"no": -1, "error": "unknown"})


def _make_chapter(tmp: Path) -> Chapter:
    folder = tmp / "ch01"
    folder.mkdir(parents=True)
    pages = []
    for i in range(1, 11):
        page = folder / f"{i:03d}.png"
        Image.new("RGB", (200, 300), (i * 25 % 255, 80, 100)).save(page)
        pages.append(page)
    return Chapter(
        key="ch01",
        title="测试漫画 第01话",
        description="贴吧简介",
        tags=["原创"],
        pages=pages,
        source_dir=folder,
        raw={},
    )


class TestTiebaPublisherMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.port}"
        cls._orig = {
            "tbs": tieba_mod.TBS_URL,
            "upload": tieba_mod.UPLOAD_URL,
            "thread": tieba_mod.THREAD_ADD_URL,
            "post": tieba_mod.POST_ADD_URL,
            "forum": tieba_mod.FORUM_URL,
        }
        tieba_mod.TBS_URL = base + "/tbs"
        tieba_mod.UPLOAD_URL = base + "/upload_image"
        tieba_mod.THREAD_ADD_URL = base + "/thread/add"
        tieba_mod.POST_ADD_URL = base + "/post/add"
        tieba_mod.FORUM_URL = base + "/f"

    @classmethod
    def tearDownClass(cls):
        tieba_mod.TBS_URL = cls._orig["tbs"]
        tieba_mod.UPLOAD_URL = cls._orig["upload"]
        tieba_mod.THREAD_ADD_URL = cls._orig["thread"]
        tieba_mod.POST_ADD_URL = cls._orig["post"]
        tieba_mod.FORUM_URL = cls._orig["forum"]
        cls.server.shutdown()

    def setUp(self):
        _Handler.log = []
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_thread_plus_replies(self):
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧", "max_pages_per_post": 3, "upload_sleep": 0, "title_suffix": "【漫画】"},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.url, "https://tieba.baidu.com/p/123")

        uploads = [r for r in _Handler.log if "upload_image" in r["path"]]
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        replies = [r for r in _Handler.log if "post/add" in r["path"]]
        self.assertEqual(len(uploads), 10)
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(replies), 3)  # 10 页 / 每楼 3 张 -> 1 楼主题 + 3 楼回复

        first = parse_qs(threads[0]["body"].decode("utf-8"))
        self.assertEqual(first["kw"][0], "漫画吧")
        self.assertTrue(first["title"][0].startswith("【漫画】"))
        self.assertIn("贴吧简介", first["content"][0])
        reply = parse_qs(replies[0]["body"].decode("utf-8"))
        self.assertEqual(reply["tid"][0], "123")
        self.assertEqual(reply["rich_text"][0], "1")


if __name__ == "__main__":
    unittest.main()

