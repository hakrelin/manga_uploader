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
    fail_thread = False
    captcha_thread = False
    thread_seq = 0
    upload_failures = 0  # 还剩几次传图要按 2230204「上传失败」拒绝

    def log_message(self, *args):
        pass

    def _reply_json(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_gbk_declared_utf8_json(self, payload: dict):
        # 复刻贴吧真实行为：Content-Type 声明 charset=GBK，正文实际是 UTF-8
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=GBK")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("/tbs"):
            self._reply_json({"is_login": 1, "tbs": "tok123"})
        elif path.endswith("/sys/user_json"):
            self._reply_gbk_declared_utf8_json(
                {
                    "tbs": "tok123",
                    "raw_name": "测试账号",
                    "id": 5504679593,
                    "creator": {
                        "name": "测试账号",
                        "name_show": "贴吧用户_abc",
                        "show_nickname": "测试昵称",
                        "id": 5504679593,
                    },
                }
            )
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
            if self.__class__.upload_failures > 0:
                self.__class__.upload_failures -= 1
                self._reply_json(
                    {
                        "error_code": "2230204",
                        "error_msg": "上传失败",
                        "info": [],
                        "server_time": "34640",
                        "time": 1788914472,
                        "ctime": 0,
                        "logid": 2472830930,
                    }
                )
                return
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
            self.__class__.thread_seq += 1
            if self.__class__.fail_thread and self.__class__.thread_seq >= 2:
                self._reply_json(
                    {
                        "no": 2000,
                        "error_code": "230871",
                        "error_msg": "发贴太频繁，请等待一段时间再试",
                        "data": {},
                    }
                )
                return
            if self.__class__.captcha_thread and self.__class__.thread_seq >= 2:
                self._reply_json(
                    {
                        "no": 2000,
                        "error_code": "230871",
                        "error_msg": "发贴太频繁",
                        "info": {"need_vcode": 1},
                        "data": {},
                    }
                )
                return
            tid = str(122 + self.__class__.thread_seq)  # 123, 124, …
            self._reply_json(
                {
                    "opgroup": "0",
                    "pid": "999",
                    "tid": tid,
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
            "sys_user": tieba_mod.SYS_USER_URL,
        }
        tieba_mod.TBS_URL = base + "/tbs"
        tieba_mod.UPLOAD_URL = base + "/uploadPicture_pc"
        tieba_mod.THREAD_ADD_URL = base + "/thread/add"
        tieba_mod.POST_ADD_URL = base + "/post/add"
        tieba_mod.FORUM_URL = base + "/f"
        tieba_mod.NEW_MOINDEX_URL = base + "/newmoindex"
        tieba_mod.SYS_USER_URL = base + "/sys/user_json"

    @classmethod
    def tearDownClass(cls):
        tieba_mod.TBS_URL = cls._orig["tbs"]
        tieba_mod.UPLOAD_URL = cls._orig["upload"]
        tieba_mod.THREAD_ADD_URL = cls._orig["thread"]
        tieba_mod.POST_ADD_URL = cls._orig["post"]
        tieba_mod.FORUM_URL = cls._orig["forum"]
        tieba_mod.NEW_MOINDEX_URL = cls._orig["newmoindex"]
        tieba_mod.SYS_USER_URL = cls._orig["sys_user"]
        cls.server.shutdown()

    def setUp(self):
        _Handler.log = []
        _Handler.forum_redirect = False
        _Handler.fail_thread = False
        _Handler.captcha_thread = False
        _Handler.thread_seq = 0
        _Handler.upload_failures = 0
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

    def test_check_login_shows_nickname(self):
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"upload_sleep": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.check()
        self.assertTrue(result.ok, result.message)
        self.assertIn("已登录", result.message)
        self.assertIn("测试昵称", result.message)
        self.assertIn("测试账号", result.message)

    def test_multi_forum_publishes_sequentially(self):
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧,东方吧", "upload_sleep": 0, "forum_interval": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        urls = result.details["urls"]
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://tieba.baidu.com/p/123")
        self.assertEqual(urls[1], "https://tieba.baidu.com/p/124")
        self.assertIn("依次", result.message)

        uploads = [r for r in _Handler.log if "uploadPicture_pc" in r["path"]]
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        replies = [r for r in _Handler.log if "post/add" in r["path"]]
        # 每个吧都独立重新传图 + 发主题帖（10 页 = 封面楼 + 1 个回复楼）
        self.assertEqual(len(uploads), 20)
        self.assertEqual(len(threads), 2)
        self.assertEqual(len(replies), 2)
        self.assertEqual(
            parse_qs(threads[0]["body"].decode("utf-8"))["kw"][0], "漫画吧"
        )
        self.assertEqual(
            parse_qs(threads[1]["body"].decode("utf-8"))["kw"][0], "东方吧"
        )
        self.assertIn("needImage", parse_qs(threads[1]["body"].decode("utf-8"))["ext"][0])

    def test_multi_forum_failure_keeps_first_result_partial(self):
        _Handler.fail_thread = True
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧,东方吧", "upload_sleep": 0, "forum_interval": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "partial", result.message)
        self.assertEqual(result.details["urls"], ["https://tieba.baidu.com/p/123"])
        self.assertIn("东方吧", result.message)
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        replies = [r for r in _Handler.log if "post/add" in r["path"]]
        uploads = [r for r in _Handler.log if "uploadPicture_pc" in r["path"]]
        # 第二个吧发主题帖失败后不再继续该吧：只上传了它的封面就停止，
        # 第一个吧的主题帖 + 回复楼完整保留
        self.assertEqual(len(threads), 2)  # 第二次请求本身失败
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(uploads), 11)

    def test_captcha_stops_later_forums(self):
        _Handler.captcha_thread = True
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={"forum": "漫画吧,东方吧,东方吧2", "upload_sleep": 0, "forum_interval": 0},
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "partial", result.message)
        self.assertIn("验证码", result.message)
        # 第二个吧触发验证码后立即停止，不再尝试第三个吧
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        self.assertEqual(len(threads), 2)


    def test_upload_retries_after_rate_limit(self):
        """贴吧限流（2230204 上传失败）：自动重试后仍然成功发帖。"""
        _Handler.upload_failures = 2
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={
                "forum": "漫画吧",
                "max_pages_per_post": 50,
                "upload_sleep": 0,
                "upload_attempts": 3,
                "upload_retry_wait": 0,
            },
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "ok", result.message)
        uploads = [r for r in _Handler.log if "uploadPicture_pc" in r["path"]]
        threads = [r for r in _Handler.log if "thread/add" in r["path"]]
        self.assertEqual(len(threads), 1)
        # 第一张图被拒 2 次后第 3 次成功，加上其余 9 张 = 12 次请求
        self.assertEqual(len(uploads), 12)

    def test_upload_failure_error_message_mentions_retry(self):
        """连续失败时给出明确中文（含重试次数），不再只报“上传失败”。"""
        _Handler.upload_failures = 99
        cfg = PlatformConfig(
            name="tieba",
            cookies={"BDUSS": "x"},
            settings={
                "forum": "漫画吧",
                "max_pages_per_post": 50,
                "upload_sleep": 0,
                "upload_attempts": 2,
                "upload_retry_wait": 0,
            },
        )
        publisher = TiebaPublisher(cfg, CommonConfig(output_dir=str(Path(self.tmp.name) / "out")))
        result = publisher.publish(_make_chapter(Path(self.tmp.name)))
        self.assertEqual(result.status, "failed")
        text = result.message
        self.assertIn("已重试 2 次", text)
        self.assertIn("限流", text)


if __name__ == "__main__":
    unittest.main()
