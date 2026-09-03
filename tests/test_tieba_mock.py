import json
import hashlib
import re
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
    forum_redirect = False

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
        elif path.endswith("/newmoindex"):
            self._reply_json(
                {
                    "no": 0,
                    "error": "success",
                    "data": {
                        "like_forum": [
                            {"forum_name": "漫画", "forum_id": 42},
                            {"forum_name": "东方", "forum_id": 71007},
                        ]
                    },
                }
            )
        elif path.endswith("/f"):
            if self.__class__.forum_redirect:
                self.send_response(302)
                self.send_header("Location", "https://passport.baidu.com/v3/login/api/auth/?tpl=tb")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
        if "uploadPicture_pc" in self.path:
            self._reply_json(
                {
                    "resourceId": "709d1d31dc47636e4f5ccbfd07601c19",
                    "chunkNo": "1",
                    "picId": "301522372501",
                    "picInfo": {
                        "originPic": {
                            "width": "200",
                            "height": "300",
                            "picUrl": "http://mock.baidu.com/a.jpg",
                        }
                    },
                    "error_code": "0",
                    "error_msg": "sucess",
                }
            )
        elif "thread/add" in self.path:
            self._reply_json(
                {
                    "opgroup": "0",
                    "pid": "999",
                    "tid": "123",
                    "msg": "发送成功",
                    "error_code": "0",
                }
            )
        elif "post/add" in self.path:
            self._reply_json(
                {
                    "opgroup": "0",
                    "pid": "456",
                    "msg": "发送成功",
                    "error_code": "0",
                }
            )
        else:
            self._reply_json({"no": -1, "error": "unknown"})


def _make_chapter(tmp: Path, count: int = 10) -> Chapter:
    folder = tmp / "ch01"
    folder.mkdir(parents=True)
    pages = []
    for i in range(1, count + 1):
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
            "newmoindex": tieba_mod.NEW_MOINDEX_URL,
        }
        tieba_mod.TBS_URL = base + "/tbs"
        tieba_mod.UPLOAD_URL = base + "/uploadPicture_pc"
        tieba_mod.THREAD_ADD_URL = base + "/thread/add"
        tieba_mod.POST_ADD_URL = base + "/post/add"
        tieba_mod.FORUM_URL = base + "/f"
        tieba_mod.NEW_MOINDEX_URL = base + "/newmoindex"

    @classmethod
    def tearDownClass(cls):
        tieba_mod.TBS_URL = cls._orig["tbs"]
        tieba_mod.UPLOAD_URL = cls._orig["upload"]
        tieba_mod.THREAD_ADD_URL = cls._orig["thread"]
        tieba_mod.POST_ADD_URL = cls._orig["post"]
        tieba_mod.FORUM_URL = cls._orig["forum"]
        tieba_mod.NEW_MOINDEX_URL = cls._orig["newmoindex"]
        cls.server.shutdown()

    def setUp(self):
        _Handler.log = []
        _Handler.forum_redirect = False
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

        uploads = [r for r in _Handler.log if "uploadPicture_pc" in r["path"]]
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        replies = [r for r in _Handler.log if "post/add" in r["path"]]
        self.assertEqual(len(uploads), 10)
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(replies), 3)  # 封面 1 楼 + 剩余 9 页每楼 3 张 -> 3 楼回复

        # 校验新版上传字段与 sign
        body = uploads[0]["body"].decode("utf-8", errors="replace")
        for field in (
            "resourceId",
            "isFinish",
            "saveOrigin",
            "size",
            "width",
            "height",
            "chunkNo",
            "pic_water_type",
            "chunk",
            "tbs",
            "subapp_type",
            "_client_type",
            "sign",
        ):
            self.assertIn(f'name="{field}"', body)
        self.assertIn('filename="001.png"', body)
        sign_match = re.search(r'name="sign"\r\n\r\n([0-9a-f]{32})', body)
        self.assertIsNotNone(sign_match, "上传 multipart 应携带 32 位 hex sign")

        first = parse_qs(threads[0]["body"].decode("utf-8"))
        self.assertEqual(first["kw"][0], "漫画吧")
        # 标题按新规则自动生成，不再追加旧式 title_suffix
        self.assertEqual(first["title"][0], "测试漫画 第01话")
        self.assertIn("贴吧简介", first["content"][0])
        self.assertIn("#(pic,301522372501,200,300)", first["content"][0])
        self.assertEqual(first["content"][0].count("#(pic,"), 1)  # 一楼只放封面
        self.assertEqual(first["is_pictxt"][0], "1")
        self.assertIn("needImage", first["ext"][0])
        reply = parse_qs(replies[0]["body"].decode("utf-8"))
        self.assertEqual(reply["tid"][0], "123")
        self.assertEqual(reply["content"][0].count("#(pic,"), 3)
        self.assertNotIn("贴吧简介", reply["content"][0])
        self.assertNotIn("rich_text", reply)

    def test_cover_first_and_nine_cap(self):
        # 配置写 50 也会被平台 9 张上限截断：封面 1 楼，19 页 -> 2 个回复楼
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧", "max_pages_per_post": 50, "upload_sleep": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        publisher.publish(_make_chapter(Path(self.tmp.name), count=19))
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        replies = [r for r in _Handler.log if "post/add" in r["path"]]
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(replies), 2)
        first = parse_qs(threads[0]["body"].decode("utf-8"))
        self.assertEqual(first["content"][0].count("#(pic,"), 1)
        for reply in replies:
            body = parse_qs(reply["body"].decode("utf-8"))
            self.assertLessEqual(body["content"][0].count("#(pic,"), 9)

    def test_pc_sign_algorithm(self):
        signed = tieba_mod._pc_sign({"b": 2, "a": "1", "chunk": tieba_mod.TIEBA_FILE_STRING})
        raw = "a=1b=2chunk=[object File]" + tieba_mod.TIEBA_PC_SIGN_SECRET
        self.assertEqual(signed["sign"], hashlib.md5(raw.encode("utf-8")).hexdigest())
        self.assertEqual(signed["a"], "1")
        self.assertEqual(signed["b"], "2")

    def test_error_classification_not_fake_vcode(self):
        # 老实现曾因响应里带 vcode 字段误报“验证码/风控”；
        # need_vcode 显式为 0 时必须按具体 error_code 归类。
        from manga_uploader.publishers.tieba import _fmt_error, _need_vcode

        payload = {
            "no": 2000,
            "err_code": 232000,
            "data": {
                "fname": "东方吧",
                "vcode": {
                    "need_vcode": 0,
                    "captcha_vcode_str": "",
                    "captcha_code_type": 0,
                },
            },
        }
        self.assertFalse(_need_vcode(payload))
        self.assertIn("内容", _fmt_error("232000", ""))
        self.assertNotIn("验证码", _fmt_error("232000", ""))

        # 真实要求验证码：need_vcode=1
        self.assertTrue(
            _need_vcode(
                {"info": {"need_vcode": "1", "vcode_md5": "abc"}}
            )
        )

    def test_build_text_uses_pic_marker(self):
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧", "upload_sleep": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        text = publisher._build_text(
            "作者：草枕\n社团：城之崎",
            [{"pic_id": "111", "width": "800", "height": "1200"}],
        )
        self.assertEqual(text, "作者：草枕\r\n社团：城之崎\r\n#(pic,111,800,1200)")
        only_pics = publisher._build_text("", [{"pic_id": "9", "width": "1", "height": "1"}])
        self.assertEqual(only_pics, "#(pic,9,1,1)")

    def test_fid_uses_followed_forum_without_forum_page(self):
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧", "max_pages_per_post": 50, "upload_sleep": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        fid = publisher._fid("漫画吧", "tok123")
        self.assertEqual(fid, "42")
        # 只走 newmoindex，没有访问吧页 /f
        self.assertFalse(any(urlparse(r["path"]).path.endswith("/f") for r in _Handler.log))

    def test_forum_redirect_gives_clear_error(self):
        _Handler.forum_redirect = True
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "不存在的吧", "max_pages_per_post": 50, "upload_sleep": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        with self.assertRaisesRegex(Exception, "fid"):
            publisher._fid("不存在的吧", "tok123")


if __name__ == "__main__":
    unittest.main()
