import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from PIL import Image

from manga_uploader.config import CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import bilibili as bili_mod
from manga_uploader.publishers.bilibili import BilibiliPublisher


class _Handler(BaseHTTPRequestHandler):
    requests_log: list = []
    draft_counter = 100

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
        if self.path.startswith("/nav"):
            self._send_json(
                {"code": 0, "message": "0", "data": {"isLogin": True, "uname": "测试", "mid": 1}}
            )
        else:
            self._send_json({"code": -404, "message": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.requests_log.append({"path": self.path, "body": body})
        if self.path.startswith("/upload-dyn"):
            self._send_json(
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": {
                        "image_url": "http://mock.hdslb.com/x.png",
                        "image_width": 100,
                        "image_height": 100,
                        "img_size": 3.2,
                    },
                }
            )
        elif self.path.startswith("/dyn"):
            self._send_json(
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": {"dyn_id": 123, "dyn_id_str": "987654321", "dyn_type": 2},
                }
            )
        elif self.path.startswith("/upimage") or self.path.startswith("/upcover"):
            self._send_json(
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": {"url": "https://mock.hdslb.com/bfs/article/xx.jpg", "size": 1024},
                }
            )
        elif self.path.startswith("/draft"):
            self.__class__.draft_counter += 1
            self._send_json(
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": {"aid": self.__class__.draft_counter},
                }
            )
        elif self.path.startswith("/submit"):
            self._send_json(
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": {"aid": self.__class__.draft_counter},
                }
            )
        else:
            self._send_json({"code": -400, "message": "bad"}, 400)


def _make_chapter(tmp: Path) -> Chapter:
    folder = tmp / "ch01"
    folder.mkdir(parents=True)
    pages = []
    for i in range(1, 11):  # 10 页
        page = folder / f"{i:03d}.png"
        Image.new("RGB", (200, 300), (i * 20 % 255, 90, 120)).save(page)
        pages.append(page)
    return Chapter(
        key="ch01",
        title="测试漫画 第01话",
        description="简介",
        tags=["原创"],
        pages=pages,
        source_dir=folder,
        raw={"platforms": {"bilibili": {"topics": ["测试话题"]}}},
    )


class TestBilibiliPublisherMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        base = f"http://127.0.0.1:{cls.port}"
        cls._orig = {
            "upload": bili_mod.DYNAMIC_UPLOAD_IMAGE_URL,
            "dyn": bili_mod.CREATE_DYN_URL,
            "nav": bili_mod.NAV_URL,
            "upimage": bili_mod.ARTICLE_UPIMAGE_URL,
            "upcover": bili_mod.ARTICLE_UPCOVER_URL,
            "draft": bili_mod.ARTICLE_DRAFT_URL,
            "submit": bili_mod.ARTICLE_SUBMIT_URL,
        }
        bili_mod.DYNAMIC_UPLOAD_IMAGE_URL = base + "/upload-dyn"
        bili_mod.CREATE_DYN_URL = base + "/dyn"
        bili_mod.NAV_URL = base + "/nav"
        bili_mod.ARTICLE_UPIMAGE_URL = base + "/upimage"
        bili_mod.ARTICLE_UPCOVER_URL = base + "/upcover"
        bili_mod.ARTICLE_DRAFT_URL = base + "/draft"
        bili_mod.ARTICLE_SUBMIT_URL = base + "/submit"

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._orig.items():
            setattr(
                bili_mod,
                {
                    "upload": "DYNAMIC_UPLOAD_IMAGE_URL",
                    "dyn": "CREATE_DYN_URL",
                    "nav": "NAV_URL",
                    "upimage": "ARTICLE_UPIMAGE_URL",
                    "upcover": "ARTICLE_UPCOVER_URL",
                    "draft": "ARTICLE_DRAFT_URL",
                    "submit": "ARTICLE_SUBMIT_URL",
                }[key],
                value,
            )
        cls.server.shutdown()

    def setUp(self):
        _Handler.requests_log = []
        _Handler.draft_counter = 100
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _publisher(self, settings: dict | None = None):
        cfg = PlatformConfig(
            name="bilibili",
            cookies={"SESSDATA": "s", "bili_jct": "csrf", "buvid3": "b"},
            settings=settings
            or {"topics": ["原创漫画"], "image_category": "draw", "publish_mode": "dynamic"},
        )
        common = CommonConfig(output_dir=str(Path(self.tmp.name) / "out"))
        return BilibiliPublisher(cfg, common)

    def _last_posts(self, path_prefix: str):
        return [r for r in _Handler.requests_log if r["path"].startswith(path_prefix)]

    def test_check(self):
        result = self._publisher().check()
        self.assertTrue(result.ok)
        self.assertIn("测试", result.message)

    def test_dynamic_publish_splits_at_nine(self):
        chapter = _make_chapter(Path(self.tmp.name))
        result = self._publisher().publish(chapter)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(len(result.details["urls"]), 2)  # 10 页拆 2 条
        self.assertIn("987654321", result.url)
        dyn_posts = self._last_posts("/dyn")
        upload_posts = self._last_posts("/upload-dyn")
        self.assertEqual(len(upload_posts), 10)
        self.assertEqual(len(dyn_posts), 2)
        first = json.loads(dyn_posts[0]["body"])
        self.assertEqual(len(first["dyn_req"]["pics"]), 9)
        self.assertEqual(first["dyn_req"]["pics"][0]["img_src"], "http://mock.hdslb.com/x.png")
        self.assertEqual(first["dyn_req"]["scene"], 2)

    def test_article_publish_single_column(self):
        chapter = _make_chapter(Path(self.tmp.name))
        result = self._publisher(
            {"publish_mode": "article", "topics": [], "image_category": "draw"}
        ).publish(chapter)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.details["mode"], "article")
        self.assertIn("cv101", result.url)

        uploads = self._last_posts("/upimage")
        drafts = self._last_posts("/draft")
        submits = self._last_posts("/submit")
        self.assertEqual(len(uploads), 10)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(len(submits), 1)
        # 顺序：全部图片 → 草稿 → 提交
        paths = [r["path"] for r in _Handler.requests_log]
        self.assertTrue(all(p.startswith("/upimage") for p in paths[:10]))
        self.assertTrue(paths[-2].startswith("/draft") and paths[-1].startswith("/submit"))

        data = parse_qs(drafts[0]["body"].decode("utf-8"))
        self.assertEqual(data["title"][0], "测试漫画 第01话")
        self.assertEqual(data["csrf"][0], "csrf")
        self.assertEqual(data["reprint"][0], "0")
        self.assertEqual(data["original"][0], "1")
        self.assertEqual(data["image_urls"][0], "https://mock.hdslb.com/bfs/article/xx.jpg")
        self.assertEqual(data["content"][0].count("<figure"), 10)
        submit_data = parse_qs(submits[0]["body"].decode("utf-8"))
        self.assertEqual(submit_data["aid"][0], "101")

    def test_article_splits_when_over_max(self):
        chapter = _make_chapter(Path(self.tmp.name))
        result = self._publisher(
            {"publish_mode": "article", "max_article_pages": 6}
        ).publish(chapter)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(len(result.details["urls"]), 2)
        self.assertEqual(len(self._last_posts("/draft")), 2)
        self.assertEqual(len(self._last_posts("/submit")), 2)
        drafts = self._last_posts("/draft")
        first_content = parse_qs(drafts[0]["body"].decode("utf-8"))["content"][0]
        second_content = parse_qs(drafts[1]["body"].decode("utf-8"))["content"][0]
        self.assertEqual(first_content.count("<figure"), 6)
        self.assertEqual(second_content.count("<figure"), 4)

    def test_article_uses_5mb_pipeline(self):
        cfg = PlatformConfig(
            name="bilibili",
            cookies={"SESSDATA": "s", "bili_jct": "csrf"},
            settings={"publish_mode": "article"},
        )
        common = CommonConfig(
            output_dir=str(Path(self.tmp.name) / "out"), max_bytes_mb=10.0
        )
        publisher = BilibiliPublisher(cfg, common)
        # 允许格式与大小：仅 jpg/png，5MB 内（article 模式通过 prepare_pages 参数落实）
        self.assertEqual(bili_mod.ARTICLE_ALLOWED_EXTS, {".jpg", ".jpeg", ".png"})
        self.assertEqual(bili_mod.ARTICLE_MAX_BYTES, 5 * 1024 * 1024)
        self.assertIsNotNone(publisher)

    def test_publish_missing_cookie(self):
        cfg = PlatformConfig(name="bilibili", cookies={})
        publisher = BilibiliPublisher(cfg, CommonConfig())
        chapter = _make_chapter(Path(self.tmp.name))
        with self.assertRaises(Exception):
            publisher.publish(chapter)


if __name__ == "__main__":
    unittest.main()
