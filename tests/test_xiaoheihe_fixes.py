"""小黑盒修复回归测试：草稿默认、正文截断、cookie jar 清理、登录失效判定。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from PIL import Image

from manga_uploader.config import DEFAULT_SETTINGS, CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import xiaoheihe as X


def _chapter(tmp: str) -> Chapter:
    return Chapter(
        key="root",
        title="测试",
        description="",
        tags=[],
        author="",
        source_dir=pathlib.Path(tmp),
        raw={"title": "测试"},
    )


class _Page:
    """_content_blocks 只需要 url/width/height 三个属性。"""

    def __init__(self, url: str, width: int, height: int) -> None:
        self.url = url
        self.width = width
        self.height = height


class XiaoheiheFixTest(unittest.TestCase):
    def test_config_default_publish_draft_is_true(self):
        # README 声明的“默认只存草稿”应与配置默认一致
        self.assertTrue(DEFAULT_SETTINGS["xiaoheihe"]["publish_draft"])

    def test_plan_reflects_draft_default_without_merged_settings(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(name="xiaoheihe", cookies={"cookie": "pkey=A"}),
            CommonConfig(output_dir=tmp),
        )
        rows = "\n".join(pub.plan(_chapter(tmp)))
        self.assertIn("默认存草稿", rows)

    def test_plan_reflects_public_when_configured_false(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(
                name="xiaoheihe",
                cookies={"cookie": "pkey=A"},
                settings={"publish_draft": False},
            ),
            CommonConfig(output_dir=tmp),
        )
        rows = "\n".join(pub.plan(_chapter(tmp)))
        self.assertIn("发布后为公开内容", rows)

    def test_description_truncated_to_server_limit(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(name="xiaoheihe", cookies={"cookie": "pkey=A"}),
            CommonConfig(output_dir=tmp),
        )
        long_text = "很长的简介\n" * 9000  # > 30000 字
        chapter = _chapter(tmp)
        chapter.raw["platforms"] = {"xiaoheihe": {"description": long_text}}
        text = pub._description(chapter)
        self.assertLessEqual(len(text), X.MAX_DESC_CHARS)

    def test_cookie_jar_drops_bogus_cookie_entry_keeps_header(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(
                name="xiaoheihe",
                cookies={
                    "cookie": "pkey=A; heybox_id=123; x_xhh_tokenid=tk1",
                    "heybox_id": "123",
                },
            ),
            CommonConfig(output_dir=tmp),
        )
        names = [c.name for c in pub.http.session.cookies]
        self.assertNotIn("cookie", names)
        self.assertIn("heybox_id", names)  # 真 cookie 保留在 jar
        self.assertIn("pkey=A", pub.http.session.headers.get("Cookie", ""))
        self.assertEqual(pub.http.session.headers.get("x-xhh-token-id"), "tk1")

    def test_login_expired_classification(self):
        # 权限类提示（含“登录”字样）不应被判成登录失效
        expired = (
            "请登录后使用该功能",
            "登录已失效",
            "非法的请求",
            "登录已过期，请重新登录",
        )
        not_expired = ("该社区需要先登录后才能发言", "内部错误", "")
        for msg in expired:
            self.assertTrue(X.XiaoheihePublisher._is_expired(msg), msg)
        for msg in not_expired:
            self.assertFalse(X.XiaoheihePublisher._is_expired(msg), msg)

    # ---------- 文章正文里的图片（“发布成功但文章没图” 回归） ----------

    def test_article_blocks_embed_images_in_html(self):
        """文章渲染只认第一个 html 块：图片必须写进 html，否则文章里没有图。"""
        pages = [_Page("https://cdn.max-c.com/a.jpg", 800, 1200),
                 _Page("https://cdn.max-c.com/b.jpg", 0, 0)]
        blocks = X._content_blocks("作者：某人\n简介：测试", pages, article=True)
        self.assertEqual(blocks[0]["type"], "html")
        body = blocks[0]["text"]
        for page in pages:
            self.assertIn(f'<img src="{page.url}"', body)
            self.assertIn(f'data-original="{page.url}"', body)
        self.assertIn("作者：某人", body)
        # 图片顺序与正文在前
        self.assertLess(body.index("作者：某人"), body.index("a.jpg"))
        # 网页端行为：后面仍然带 img 块（图片列表/计数用）
        self.assertEqual([b["type"] for b in blocks[1:]], ["img", "img"])

    def test_image_text_blocks_stay_text_plus_img(self):
        """图文模式不受影响：text 块 + img 块（渲染端会自己拼成 <img>）。"""
        pages = [_Page("https://cdn.max-c.com/a.jpg", 10, 20)]
        blocks = X._content_blocks("正文", pages, article=False)
        self.assertEqual([b["type"] for b in blocks], ["text", "img"])
        self.assertNotIn("<img", blocks[0]["text"])

    def test_article_without_description_still_carries_images(self):
        pages = [_Page("https://cdn.max-c.com/a.jpg", 10, 20)]
        blocks = X._content_blocks("", pages, article=True)
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn("cdn.max-c.com/a.jpg", blocks[0]["text"])

    def test_img_html_escapes_url_and_keeps_size(self):
        page = _Page("https://cdn.max-c.com/a.jpg?x=1&y=2", 640, 960)
        tag = X._img_html(page)
        self.assertIn("x=1&amp;y=2", tag)
        self.assertIn('data-width="640"', tag)
        self.assertIn('data-height="960"', tag)


class _XhhHandler(BaseHTTPRequestHandler):
    """只回帖子/评论创建接口的假小黑盒服务端。"""

    posts: list = []
    link_counter = 0
    comment_counter = 0

    def log_message(self, *args):
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        path = self.path.split("?")[0]
        self.__class__.posts.append({"path": path, "query": self.path, "body": body})
        if path == X.POST_URL:
            self.__class__.link_counter += 1
            self._json({"status": "ok", "msg": "", "link_id": 100000 + self.__class__.link_counter})
        elif path == X.COMMENT_CREATE_URL:
            self.__class__.comment_counter += 1
            self._json(
                {
                    "status": "ok",
                    "msg": "",
                    "result": {"comment": [{"commentid": 900000 + self.__class__.comment_counter}]},
                }
            )
        else:
            self._json({"status": "failed", "msg": f"unknown path {path}"})


class XiaoheiheOverflowCommentTest(unittest.TestCase):
    """超过单帖上限的图不再另发一帖，而是发到首帖评论区。"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _XhhHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._orig_api = X.API
        X.API = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        X.API = cls._orig_api
        cls.server.shutdown()

    def setUp(self):
        _XhhHandler.posts = []
        _XhhHandler.link_counter = 0
        _XhhHandler.comment_counter = 0
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _chapter(self, count: int) -> Chapter:
        folder = Path(self.tmp.name) / "ch01"
        folder.mkdir(parents=True, exist_ok=True)
        pages = []
        for i in range(1, count + 1):
            page = folder / f"{i:03d}.png"
            Image.new("RGB", (40, 60), (i, 60, 90)).save(page)
            pages.append(page)
        return Chapter(
            key="ch01",
            title="评论续图测试",
            description="简介",
            tags=[],
            pages=pages,
            source_dir=folder,
            raw={"title": "评论续图测试"},
        )

    def _publisher(self, **settings):
        base = {
            "publish_mode": "article",
            "publish_draft": False,
            "article_max_pages": 5,
            "comment_max_pages": 2,
            "topic_ids": "",
            "hashtags": "",
        }
        base.update(settings)
        return X.XiaoheihePublisher(
            PlatformConfig(name="xiaoheihe", cookies={"cookie": "pkey=A"}, settings=base),
            CommonConfig(output_dir=str(Path(self.tmp.name) / "out")),
        )

    def _run(self, publisher, chapter):
        uploaded = {"n": 0}

        def fake_upload(page):
            uploaded["n"] += 1
            return X._UploadedPage(f"https://img.example.com/{Path(page.path).name}", 40, 60)

        with mock.patch.object(X.XiaoheihePublisher, "_upload_page", side_effect=fake_upload):
            return publisher.publish(chapter)

    def test_overflow_pages_go_to_comments(self):
        # 12 页、单帖上限 5、每条评论 2 张 → 1 帖（5）+ 评论区 4 条（2/2/2/1）
        result = self._run(self._publisher(), self._chapter(12))
        self.assertEqual(result.status, "ok", result.message)
        self.assertIn("评论区", result.message)
        self.assertEqual(result.details.get("comments"), 4)

        post_calls = [p for p in _XhhHandler.posts if p["path"] == X.POST_URL]
        comment_calls = [p for p in _XhhHandler.posts if p["path"] == X.COMMENT_CREATE_URL]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(len(comment_calls), 4)
        # 评论参数对齐网页端：rnd=15 & target=heybox_app，顶层评论 root_id/reply_id 都是 -1
        # 评论接口的签名参数：_rnd=15:hmac（少了它服务端回「帖子id错误/缺失参数」）
        self.assertIn("_rnd=15%3A", comment_calls[0]["query"])
        self.assertNotIn("rnd=15&", comment_calls[0]["query"])
        first = comment_calls[0]["body"]
        self.assertIn("link_id=100001", first)
        self.assertIn("root_id=-1", first)
        self.assertIn("reply_id=-1", first)
        # imgs 用分号连接；第 2 条评论对应第 8–9 页
        self.assertEqual(first.count("img.example.com"), 2)
        self.assertIn("%3B", first)  # 分号被 urlencode（两条图之间）
        self.assertIn("006", comment_calls[0]["body"] + comment_calls[0]["query"])
        self.assertIn("008", comment_calls[1]["body"] + comment_calls[1]["query"])
        self.assertIn("009", comment_calls[1]["body"] + comment_calls[1]["query"])
        # 最后一条只带剩下 1 张
        self.assertEqual(comment_calls[-1]["body"].count("img.example.com"), 1)

    def test_overflow_mode_post_keeps_old_behaviour(self):
        result = self._run(self._publisher(overflow_mode="post"), self._chapter(12))
        self.assertEqual(result.status, "ok", result.message)
        post_calls = [p for p in _XhhHandler.posts if p["path"] == X.POST_URL]
        comment_calls = [p for p in _XhhHandler.posts if p["path"] == X.COMMENT_CREATE_URL]
        self.assertEqual(len(post_calls), 3)   # 5 + 5 + 2
        self.assertEqual(len(comment_calls), 0)

    def test_draft_mode_publishes_as_self_only_then_comments(self):
        """草稿不能评论（站点限制）：超上限时改按「仅自己可见」发布，再补评论区。"""
        result = self._run(self._publisher(publish_draft=True), self._chapter(9))
        self.assertEqual(result.status, "ok", result.message)
        self.assertIn("仅自己可见", result.message)
        post_calls = [p for p in _XhhHandler.posts if p["path"] == X.POST_URL]
        comment_calls = [p for p in _XhhHandler.posts if p["path"] == X.COMMENT_CREATE_URL]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(len(comment_calls), 2)
        # 不是草稿（draft=1），而是 view_limit=3 的正式帖
        self.assertIn("view_limit=3", post_calls[0]["body"])
        self.assertNotIn("draft=1", post_calls[0]["body"])

    def test_rnd_matches_captured_request(self):
        """评论接口的 _rnd 参数与网页端抓包逐字节一致（少了它必然失败）。"""
        self.assertEqual(
            X._rnd("D9A6041439A823F2299AA1B62ED92B5A", 1789720373),
            "15:2b41b192de39cf6ef411addfde0c065a853bfa358d9ce151237b238fb285d65b",
        )
        url = X._signed_url(X.COMMENT_CREATE_URL, user_id="1", extra={"rnd": "15"})
        self.assertIn("_rnd=15%3A", url)
        self.assertNotIn("&rnd=15", url)

    def test_comment_failure_marks_partial(self):
        class _Boom(_XhhHandler):
            def do_POST(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == X.COMMENT_CREATE_URL:
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    self.__class__.posts.append({"path": path, "query": self.path, "body": ""})
                    self._json({"status": "failed", "msg": "帖子不存在"})
                    return
                super().do_POST()

        server = HTTPServer(("127.0.0.1", 0), _Boom)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        orig = X.API
        X.API = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            result = self._run(self._publisher(), self._chapter(9))
        finally:
            X.API = orig
            server.shutdown()
        self.assertEqual(result.status, "partial")
        self.assertIn("评论区", result.message)
        self.assertIn("帖子不存在", result.message)


if __name__ == "__main__":
    unittest.main()
