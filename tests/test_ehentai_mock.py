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
<form id="uploadform" method="post" enctype="multipart/form-data" action="/upload">
  <input type="hidden" name="MAX_FILE_SIZE" value="1258291200">
  <input type="hidden" name="PHP_SESSION_UPLOAD_PROGRESS" value="sesstoken123">
  <input type="hidden" name="do_save" value="1">
  <input type="text" id="gname_en" name="gname_en" maxlength="500">
  <input type="text" id="gname_jp" name="gname_jp" maxlength="500">
  <select name="category">
    <option value="2" selected>Doujinshi</option>
    <option value="3">Manga</option>
    <option value="9">Non-H</option>
  </select>
  <select name="langtag">
    <option value="0" selected>Japanese / No Text</option>
    <option value="3437">Chinese</option>
    <option value="1058">English</option>
  </select>
  <input type="radio" name="langtype" value="0" checked>
  <input type="radio" name="langtype" value="1">
  <input type="radio" name="langtype" value="2">
  <input type="checkbox" name="langctl">
  <select name="folderid">
    <option value="1">个人文件夹</option>
    <option value="0" selected>Unsorted</option>
  </select>
  <textarea name="ulcomment"></textarea>
  <input type="text" name="some_unknown_field" value="do_not_copy_me">
  <input type="checkbox" name="tos">
  <input type="file" id="uploadfiles" name="files[]" multiple>
</form>
</body></html>"""

SUCCESS_HTML = """<html><body><a href="https://e-hentai.org/g/abcdef0123456789/1/">view gallery</a></body></html>"""
HOME_HTML = """<html><body><h1>E-Hentai Galleries</h1>
<p>Found 1,609,366 results.</p></body></html>"""
DRAFT_HTML = """<html><body>
<form id="uploadform" action="/upload?ulgid=99999" method="post" enctype="multipart/form-data">
<input type="hidden" name="do_save" value="1">
<h2>测试漫画 第01话</h2>
<td class="v">11</td>
<td class="v">No (Unpublished)</td>
<input id="pagesel_1" name="pagesel_1" type="text" value="1">
<input id="pagesel_2" name="pagesel_2" type="text" value="2">
<input id="pagesel_3" name="pagesel_3" type="text" value="3">
<input id="pagesel_4" name="pagesel_4" type="text" value="4">
<input id="pagesel_5" name="pagesel_5" type="text" value="5">
</form>
<div id="progress_readout"><p>Added <strong>5</strong> new images to the gallery.</p></div>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    posts: list = []
    gets: list = []
    page_html = UPLOAD_HTML
    redirect_url = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = self.__class__
        cls.gets.append(self.path)
        if "act=publish" in self.path:
            body = SUCCESS_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
        out = DRAFT_HTML.encode("utf-8")
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
        _Handler.gets = []
        _Handler.page_html = UPLOAD_HTML
        _Handler.redirect_url = ""
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish(self):
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={
                "category_label": "Manga",
                "language_label": "Chinese",
                "upload_mode": "files",  # 逐张上传路径的回归测试
            },
        )
        publisher = EhentaiPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.url, "https://e-hentai.org/g/abcdef0123456789/1")

        self.assertEqual(len(_Handler.posts), 1)
        body = _Handler.posts[0]["body"]
        self.assertIn(b'name="gname_en"', body)
        self.assertIn("测试漫画 第01话".encode("utf-8"), body)
        self.assertIn(b'name="ulcomment"', body)
        self.assertIn("EH 简介".encode("utf-8"), body)
        self.assertIn(b'name="files[]"', body)
        self.assertEqual(body.count(b'name="files[]"'), 5)
        # 真实字段：分类 Manga value=3；语言保留页面默认 Japanese/No Text；勾选 TOS
        self.assertIn(b'name="category"\r\n\r\n3\r\n', body)
        self.assertIn(b'name="langtag"\r\n\r\n3437\r\n', body)
        # 汉化默认：langtype=1(Translated)，并勾选专业翻译者 langctl
        self.assertIn(b'name="langtype"\r\n\r\n1\r\n', body)
        self.assertIn(b'name="langctl"\r\n\r\non\r\n', body)
        self.assertIn(b'name="tos"\r\n\r\non\r\n', body)
        # 隐藏字段照常回传（会话进度、文件大小限制）
        self.assertIn(b"sesstoken123", body)
        self.assertIn(b"1258291200", body)
        self.assertIn(b"do_save", body)
        # 未映射的未知文本框不自动填默认值
        self.assertNotIn(b'name="some_unknown_field"', body)
        self.assertNotIn(b"do_not_copy_me", body)

    def test_publish_default_zip_single_archive(self):
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={"category_label": "Manga", "language_label": "Chinese"},
        )
        publisher = EhentaiPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(len(_Handler.posts), 1)
        body = _Handler.posts[0]["body"]
        # zip 模式：只发一个 files[] 归档（application/zip），内含 5 张页面
        self.assertEqual(body.count(b'name="files[]"'), 1)
        self.assertIn(b"application/zip", body)
        self.assertIn(b'filename="gallery.zip"', body)
        # 归档内按页序命名（01.png … 05.png）
        self.assertIn(b"01.png", body)
        self.assertIn(b"05.png", body)

    def test_publish_platform_override_second_title(self):
        chapter = _make_chapter(Path(self.tmp.name))
        chapter.raw["platforms"] = {
            "ehentai": {
                "title_jpn": "エロマンガ第01話",
                "gname_jp": "エロマンガ第01話",
                "category": "Manga",
            }
        }
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={
                "category_label": "Manga",
            },
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        result = publisher.publish(chapter)
        self.assertEqual(result.status, "ok", result.message)
        body = _Handler.posts[0]["body"]
        self.assertIn(b'name="gname_en"', body)
        self.assertIn(b'name="gname_jp"', body)
        self.assertIn("エロマンガ第01話".encode("utf-8"), body)
        # 平台覆盖的整段 gname_jp 原样提交（不再二次拼接）
        self.assertNotIn("[中国翻訳]".encode("utf-8"), body)
        self.assertIn(b'name="tos"\r\n\r\non\r\n', body)
        # 未映射的未知文本框不再被自动填入默认值
        self.assertNotIn(b'name="some_unknown_field"', body)
        self.assertNotIn(b"do_not_copy_me", body)

    def test_publish_draft_only_when_disabled(self):
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={
                "category_label": "Manga",
                "language_label": "Chinese",
                "publish_after_upload": False,
            },
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertIn("ulgid=99999", result.url)
        self.assertIn("草稿", result.message)
        self.assertFalse(any("act=publish" in path for path in _Handler.gets))

    def test_publish_auto_publishes_draft(self):
        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={"category_label": "Manga", "language_label": "Chinese"},
        )
        publisher = EhentaiPublisher(
            cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        )
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.url, "https://e-hentai.org/g/abcdef0123456789/1")
        self.assertTrue(any("act=publish" in path for path in _Handler.gets))

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
