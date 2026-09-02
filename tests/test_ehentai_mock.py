import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from manga_uploader.config import CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import ehentai as eh_mod
from manga_uploader.publishers.ehentai import EhentaiPublisher

UPLOAD_HTML = """<!DOCTYPE html><html><body>
<form method="post" enctype="multipart/form-data" action="/submit">
  <input type="hidden" name="noscript" value="1">
  <input type="text" name="name" maxlength="255">
  <textarea name="comment"></textarea>
  <select name="category">
    <option value="0">Doujinshi</option>
    <option value="1">Manga</option>
  </select>
  <select name="rating">
    <option value="s">Safe</option>
  </select>
  <select name="language">
    <option value="zh">Chinese (Simplified)</option>
  </select>
  <input type="text" name="tags">
  <input type="file" name="sfile[]" multiple>
</form>
</body></html>"""

SUCCESS_HTML = """<html><body><a href="https://e-hentai.org/g/abcdef0123456789/1/">view gallery</a></body></html>"""
HOME_HTML = """<html><body><h1>E-Hentai Galleries</h1>
<p>Found 1,609,366 results.</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    posts: list = []
    page_html = UPLOAD_HTML
    redirect_url = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = self.__class__
        if self.path.startswith("/upload") and cls.redirect_url:
            self.send_response(302)
            self.send_header("Location", cls.redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/home"):
            body = HOME_HTML.encode("utf-8")
        else:
            body = cls.page_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.posts.append({"path": self.path, "body": body})
        out = SUCCESS_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _make_chapter(tmp: Path) -> Chapter:
    folder = tmp / "ch01"
    folder.mkdir(parents=True)
    pages = []
    for i in range(1, 6):
        page = folder / f"{i:03d}.png"
        Image.new("RGB", (800, 1200), (60 + i * 10, 80, 100)).save(page)
        pages.append(page)
    return Chapter(
        key="ch01",
        title="测试漫画 第01话",
        description="EH 简介",
        tags=["原创", "parody:original"],
        pages=pages,
        source_dir=folder,
        raw={"platforms": {"ehentai": {"category": "Manga", "extra_tags": ["artist:someone"]}}},
    )


class TestEhentaiPublisherMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._orig = eh_mod.UPLOAD_PAGE_URL
        eh_mod.UPLOAD_PAGE_URL = f"http://127.0.0.1:{cls.port}/upload"

    @classmethod
    def tearDownClass(cls):
        eh_mod.UPLOAD_PAGE_URL = cls._orig
        cls.server.shutdown()

    def setUp(self):
        _Handler.posts = []
        _Handler.page_html = UPLOAD_HTML
        _Handler.redirect_url = ""
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish(self):
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={"category_label": "Manga"},
        )
        publisher = EhentaiPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.url, "https://e-hentai.org/g/abcdef0123456789/1")

        self.assertEqual(len(_Handler.posts), 1)
        body = _Handler.posts[0]["body"]
        self.assertIn(b'name="name"', body)
        self.assertIn("测试漫画 第01话".encode("utf-8"), body)
        self.assertIn("EH 简介".encode("utf-8"), body)
        self.assertIn(b'name="sfile[]"', body)
        self.assertEqual(body.count(b'name="sfile[]"'), 5)
        self.assertIn(b"language:chinese", body)
        self.assertIn(b"artist:someone", body)
        # 分类选到 Manga 的 value=1
        self.assertIn(b'name="category"\r\n\r\n1\r\n', body)

    def test_check_dumps_mismatched_page(self):
        _Handler.page_html = "<html><body>这是一个新版页面，没有上传表单</body></html>"
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        result = publisher.check()
        self.assertFalse(result.ok)
        self.assertIn("ehentai-check-page", result.message)
        dumps = list((Path(self.tmp.name) / "out" / "debug").glob("ehentai-check-page*"))
        self.assertEqual(len(dumps), 1)
        self.assertIn("新版页面", dumps[0].read_text(encoding="utf-8"))

    def test_publish_dumps_mismatched_page(self):
        _Handler.page_html = "<html><body>没有表单的响应</body></html>"
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        with self.assertRaisesRegex(Exception, "ehentai-upload-page"):
            publisher.publish(_make_chapter(Path(self.tmp.name)))
        dumps = list((Path(self.tmp.name) / "out" / "debug").glob("ehentai-upload-page*"))
        self.assertEqual(len(dumps), 1)
        self.assertIn("没有表单", dumps[0].read_text(encoding="utf-8"))

    def test_check_detects_redirect_to_main_site(self):
        _Handler.redirect_url = "http://127.0.0.1:%d/home" % TestEhentaiPublisherMock.port
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        result = publisher.check()
        self.assertFalse(result.ok)
        self.assertIn("跳转", result.message)
        self.assertIn("/home", result.message)
        dumps = list((Path(self.tmp.name) / "out" / "debug").glob("ehentai-check-page*"))
        self.assertEqual(len(dumps), 1)

    def test_publish_detects_redirect_to_main_site(self):
        _Handler.redirect_url = "http://127.0.0.1:%d/home" % TestEhentaiPublisherMock.port
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        with self.assertRaisesRegex(Exception, "跳转"):
            publisher.publish(_make_chapter(Path(self.tmp.name)))
        dumps = list((Path(self.tmp.name) / "out" / "debug").glob("ehentai-upload-page*"))
        self.assertEqual(len(dumps), 1)


if __name__ == "__main__":
    unittest.main()
