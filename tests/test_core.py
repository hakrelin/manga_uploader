import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image

from manga_uploader.comic import load_chapters, platform_meta
from manga_uploader.comic import page_sequence_warnings
from manga_uploader.config import load_config, missing_cookies
from manga_uploader.config import CommonConfig, PlatformConfig
from manga_uploader.web import _book_to_compose, _save_comic_meta
from manga_uploader.publishers.ehentai import _parse_upload_page
from manga_uploader.publishers.bilibili import BilibiliPublisher
from manga_uploader.publishers.tieba import _find_first
from manga_uploader import http_client
from manga_uploader.http_client import _clean_proxy_url, detect_system_proxy
from manga_uploader.util import prepare_page
from manga_uploader import __version__, build_stamp, git_revision


class TestComicScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 在临时副本上跑：examples/my_comic 是给人试用的样例，用户在里面点过
        # “保存”之后会生成章节级 manga.json（ch01/manga.json），那是用户数据，
        # 不该让测试变红。这里只保留随仓库发布的那份样例（根目录 manga.json）。
        self.demo = Path(self.tmp.name) / "my_comic"
        shutil.copytree("examples/my_comic", self.demo)
        for extra in self.demo.glob("*/manga.json"):
            extra.unlink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_two_chapters(self):
        chapters = load_chapters(self.demo)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0].key, "ch01")
        self.assertTrue(chapters[0].title.startswith("示例漫画"))
        self.assertEqual(chapters[0].raw.get("series_title"), "示例漫画")
        self.assertGreaterEqual(len(chapters[0].pages), 3)
        self.assertEqual(chapters[0].description, "第一话：主角登场。")

    def test_platform_meta(self):
        chapters = load_chapters(self.demo, only_chapters=["ch01"])
        meta = platform_meta(chapters[0], "tieba")
        self.assertEqual(meta["forum"], "请改成你的目标吧名")

    def test_filter_missing_chapter(self):
        with self.assertRaises(RuntimeError):
            load_chapters(self.demo, only_chapters=["nope"])

    def test_flat_image_folder_single_title(self):
        folder = Path(self.tmp.name) / "某漫画第1话"
        folder.mkdir()
        for i in range(1, 4):
            page = folder / f"{i:02d}.png"
            Image.new("RGB", (30, 40), "blue").save(page)
        chapters = load_chapters(folder)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].key, "root")
        self.assertEqual(chapters[0].title, "某漫画第1话")

    def test_page_sequence_warnings(self):
        from manga_uploader.comic import page_sequence_warnings

        folder = Path(self.tmp.name) / "warn"
        folder.mkdir()
        names = ["001.png", "002.png", "002.jpg", "004.png", "pic.png"]
        for name in names:
            Image.new("RGB", (20, 20), "red").save(folder / name)
        warnings = page_sequence_warnings(
            sorted((folder / n) for n in names)
        )
        joined = "\n".join(warnings)
        self.assertIn("同名文件", joined)
        self.assertIn("缺口", joined)


class TestConfig(unittest.TestCase):
    def setUp(self):
        # 同 TestComicScan：用临时副本，避免用户按了“保存”的样例影响断言
        self.tmp = tempfile.TemporaryDirectory()
        self.demo = Path(self.tmp.name) / "my_comic"
        shutil.copytree("examples/my_comic", self.demo)
        for extra in self.demo.glob("*/manga.json"):
            extra.unlink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_example(self):
        cfg = load_config("config.example.yaml")
        self.assertIn("bilibili", cfg.platforms)
        self.assertIn("zaimanhua", cfg.platforms)
        self.assertTrue(cfg.platforms["bilibili"].enabled)
        self.assertEqual(cfg.platforms["bilibili"].get("max_pages_per_post"), 9)
        self.assertEqual(cfg.platforms["bilibili"].get("publish_mode"), "article")
        self.assertAlmostEqual(cfg.common.max_bytes_mb, 10.0)
        # 代理全自动：示例配置里不再有逐平台的代理项
        for key in ("bilibili", "tieba", "ehentai", "xiaoheihe", "zaimanhua"):
            self.assertIsNone(cfg.platforms[key].get("proxy_url"), key)
            self.assertIsNone(cfg.platforms[key].get("use_system_proxy"), key)
        self.assertFalse(cfg.common.ai_enabled)
        self.assertEqual(cfg.common.ai_timeout, 60.0)

    def test_missing_cookies_by_platform(self):
        from manga_uploader.config import PlatformConfig

        ehentai = PlatformConfig(name="ehentai", cookies={"ipb_member_id": "1"})
        self.assertEqual(missing_cookies(ehentai), ["ipb_pass_hash"])
        zaimanhua = PlatformConfig(name="zaimanhua", cookies={})
        self.assertEqual(missing_cookies(zaimanhua), ["token"])
        bilibili = PlatformConfig(name="bilibili", cookies={"SESSDATA": "s", "bili_jct": "c"})
        self.assertEqual(missing_cookies(bilibili), [])

    def test_book_to_compose_series_only_no_crash(self):
        """回归：系列预填但日文标题/作者/社团/展会全空时，
        ehentai_title_jp 的 parts 为空，parts[-1] 曾 IndexError（/api/compose 500）。"""
        book = {
            "title": "测试本",
            "series": "东方",
            "series_jp": "東方Project",
            "series_en": "Touhou Project",
            "language": "Chinese",
        }
        out = _book_to_compose(str(self.demo), book)
        eh = out["platforms_content"]["ehentai"]
        self.assertIn("(東方Project)", eh["gname_jp"])
        self.assertIn("(Touhou Project)", eh["gname_en"])

    def test_book_to_compose_local_romaji_and_default_language(self):
        book = {
            "title": "魔理沙啊愿你安息",
            "author": "加陽きら",
            "circle": "まっしろけ",
            "event": "例大祭22",
            "group": "茶与金平糖汉化组",
            "series": "东方",
            "series_jp": "東方Project",
            "series_en": "Touhou Project",
            "title_jp": "マリサよ安らかに",
            "description": "简介",
            "tags": "东方,汉化",
        }
        out = _book_to_compose(str(self.demo), book)
        # 语言留空默认 Chinese；罗马音由本地引擎自动生成
        self.assertEqual(out["language"], "Chinese")
        self.assertEqual(out["romaji"]["author_en"], "Kayou Kira")
        self.assertIn("Reitaisai 22", out["romaji"]["event_en"])
        self.assertEqual(out["romaji"]["title_en"], "Marisa Yo Yasura Kani")
        # 平台发布内容按漫画信息组合
        bili = out["platforms_content"]["bilibili"]
        self.assertEqual(bili["title"], "【茶与金平糖汉化组】魔理沙啊愿你安息")
        self.assertIn("作者：加陽きら", bili["description"])
        self.assertIn("[Chinese]", out["platforms_content"]["ehentai"]["gname_en"])

    def test_book_to_compose_auto_ignores_stored_overrides(self):
        """platforms_auto = 纯按漫画信息组合（不参考已存的平台覆盖），
        前端用它区分“自动内容”和“手写覆盖”。"""
        (self.demo / "manga.json").write_text(
            json.dumps(
                {
                    "title": "新标题",
                    "author": "新作者",
                    "description": "新简介",
                    "group": "新汉化组",
                    "tags": ["东方", "汉化"],
                    "platforms": {
                        "bilibili": {
                            "title": "【旧汉化组】旧标题",
                            "description": "作者：旧作者\n简介：旧简介",
                            "tags": "旧标签",
                            "list_name": "旧文集",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out = _book_to_compose(str(self.demo), {})
        # 展示值（platforms_content）仍带已存覆盖
        self.assertEqual(out["platforms_content"]["bilibili"]["title"], "【旧汉化组】旧标题")
        # 自动值（platforms_auto）忽略覆盖，按漫画信息重新算
        auto = out["platforms_auto"]["bilibili"]
        self.assertEqual(auto["title"], "【新汉化组】新标题")
        self.assertIn("作者：新作者", auto["description"])
        self.assertEqual(auto["tags"], "东方, 汉化")
        # 非“组合出来”的字段（文集、吧名等）不给自动值，前端不会拿它覆盖
        self.assertEqual(auto["list_name"], "")

    def test_save_comic_meta_drops_auto_equal_values(self):
        """保存时把“与自动组合一致”的平台字段丢掉：它们不是手写覆盖，
        写进 manga.json 会固化成旧值（这正是“改了信息却不更新”的根因）。"""
        path = _save_comic_meta(
            str(self.demo),
            {"title": "示例漫画", "author": "作者名"},
            {
                "bilibili": {
                    # 与自动组合一致 → 不写
                    "title": "示例漫画",
                    # 手写的正文 → 保留
                    "description": "这是我自己手写的正文。",
                }
            },
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        bili = data["platforms"]["bilibili"]
        self.assertNotIn("title", bili)
        self.assertEqual(bili["description"], "这是我自己手写的正文。")

    def test_save_comic_meta_clears_stale_auto_override(self):
        """老版本写进去的“自动值覆盖”会在下次保存时被清掉（自愈）。"""
        meta_file = self.demo / "manga.json"
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        data["platforms"]["bilibili"] = {"title": "示例漫画"}
        meta_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = _book_to_compose(str(self.demo), {"title": "示例漫画"})
        auto_title = out["platforms_auto"]["bilibili"]["title"]
        # 存的就是“当时自动组合出来的标题” → 保存时应被清掉
        path = _save_comic_meta(
            str(self.demo), {"title": "示例漫画"}, {"bilibili": {"title": auto_title}}
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("title", saved["platforms"]["bilibili"])

    def test_save_config_accepts_frontend_wrapped_payload(self):
        """前端 POST /api/config 发送 {config:{common,platforms}}；
        保存必须真实落盘（回归：曾因未解包 config 而静默无效）。"""
        import tempfile
        from pathlib import Path

        from manga_uploader.webui import save_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "common:\n  max_bytes_mb: 10\nplatforms:\n"
                "  ehentai:\n    enabled: false\n    settings:\n"
                "      category_label: Doujinshi\n",
                encoding="utf-8",
            )
            wrapped = {
                "config": {
                    "common": {"max_bytes_mb": 25},
                    "platforms": {
                        "ehentai": {
                            "enabled": True,
                            "cookies": {"ipb_member_id": "1"},
                            "settings": {"category_label": "Manga"},
                        }
                    },
                }
            }
            # 与 _api_config 修复一致：先取 data["config"] 再保存
            payload = wrapped.get("config")
            save_config(cfg_path, payload)
            text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("max_bytes_mb: 25", text)
            self.assertIn("enabled: true", text)
            self.assertIn("category_label: Manga", text)
            reloaded = load_config(str(cfg_path))
            self.assertTrue(reloaded.platforms["ehentai"].enabled)
            self.assertEqual(
                reloaded.platforms["ehentai"].get("category_label"), "Manga"
            )


class TestEhentaiFormParser(unittest.TestCase):
    HTML = """
    <html><body>
    <form id="logout" method="post"><input type="submit"></form>
    <form method="post" enctype="multipart/form-data" action="/">
      <input type="hidden" name="noscript" value="1">
      <input type="text" name="name" maxlength="255">
      <textarea name="comment"></textarea>
      <select name="category">
        <option value="">---</option>
        <option value="2">Manga</option>
        <option value="3">Non-H</option>
      </select>
      <select name="language">
        <option value="zh">Chinese (Simplified)</option>
      </select>
      <input type="text" name="tags">
      <input type="file" name="sfile[]" multiple>
    </form>
    </body></html>
    """

    def test_parse(self):
        form = _parse_upload_page(self.HTML)
        self.assertIsNotNone(form)
        self.assertEqual(form.action, "/")
        self.assertTrue(form.has("sfile[]"))
        cat = form.by_name("category")
        self.assertEqual([label for _, label in cat.options], ["---", "Manga", "Non-H"])


class TestHelpers(unittest.TestCase):
    def test_find_first_nested(self):
        payload = {"info": {"imgurl": "http://x/y.jpg"}}
        self.assertEqual(_find_first(payload, ("imgurl", "url")), "http://x/y.jpg")

    def test_empty_cookie_values_are_not_sent(self):
        """配置界面留空的 Cookie 不能进 jar，否则自动补全会重复下发。"""
        from manga_uploader.http_client import HttpClient

        client = HttpClient(cookies={"SESSDATA": "s", "buvid4": "", "b_nut": "   "})
        self.assertEqual(client.session.cookies.get("SESSDATA"), "s")
        self.assertIsNone(client.session.cookies.get("buvid4"))
        self.assertIsNone(client.session.cookies.get("b_nut"))
        import requests

        prepared = client.session.prepare_request(
            requests.Request("GET", "https://api.bilibili.com/x/web-interface/nav")
        )
        header = prepared.headers.get("Cookie", "")
        self.assertEqual(header, "SESSDATA=s")
        self.assertNotIn("buvid4", header)

    def test_cookie_diff_flags_conflicting_accounts(self):
        """页面配置与 config.yaml 的 Cookie 不同时要能发现（发错账号的根源）。"""
        from manga_uploader.web import _cookie_diff

        page = {
            "platforms": {
                "tieba": {"cookies": {"BDUSS": "page-bduss"}},
                "bilibili": {"cookies": {"SESSDATA": "same", "bili_jct": ""}},
                "ehentai": {"cookies": {"ipb_member_id": "1"}},
            }
        }
        disk = {
            "platforms": {
                "tieba": {"cookies": {"BDUSS": "disk-bduss"}},
                "bilibili": {"cookies": {"SESSDATA": "same"}},
                "ehentai": {"cookies": {}},
            }
        }
        diff = _cookie_diff(page, disk)
        self.assertEqual(diff, {"tieba": ["BDUSS"]})
        self.assertEqual(_cookie_diff(page, page), {})

    def test_bilibili_card_exposes_device_cookie_fields(self):
        """配置界面要能填 B站风控依赖的 buvid3/buvid4/b_nut/DedeUserID。"""
        from manga_uploader.webui import PLATFORM_CARDS

        card = next(c for c in PLATFORM_CARDS if c["key"] == "bilibili")
        names = [f["name"] for f in card["cookie_fields"]]
        for name in ("SESSDATA", "bili_jct", "buvid3", "buvid4", "b_nut", "DedeUserID"):
            self.assertIn(name, names)
        required = [f["name"] for f in card["cookie_fields"] if f.get("required")]
        self.assertEqual(required, ["SESSDATA", "bili_jct"])

    def test_clean_proxy_url(self):
        self.assertEqual(_clean_proxy_url("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(_clean_proxy_url("http=http://a:1;https=http://b:2"), "http://a:1")
        self.assertEqual(_clean_proxy_url(""), "")

    def test_detect_proxy_from_env(self):
        import manga_uploader.http_client as hc

        with unittest.mock.patch.dict(
            hc.os.environ,
            {"HTTP_PROXY": "", "HTTPS_PROXY": "http://proxy.local:8888"},
            clear=False,
        ):
            self.assertEqual(detect_system_proxy(), "http://proxy.local:8888")

    def test_prepare_page_converts_webp(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "page.webp"
            Image.new("RGB", (100, 100), "white").save(src)
            out = prepare_page(
                src,
                tmp_path / "out",
                allowed_exts={".jpg"},
                max_width=0,
            )
            self.assertEqual(out.path.suffix, ".jpg")
            self.assertEqual(out.width, 100)

    def test_prepare_page_keeps_original(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "page.png"
            Image.new("RGB", (200, 300), "white").save(src)
            out = prepare_page(src, tmp_path / "out", allowed_exts={".png"})
            self.assertEqual(out.path, src)

    def test_prepare_page_never_crops(self):
        """等比缩放：超宽图按最长边限制只缩小，不裁成方形。"""
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "wide.png"
            Image.new("RGB", (1000, 400), "red").save(src)
            item = prepare_page(
                src,
                tmp_path / "out",
                allowed_exts={".jpg"},
                max_width=200,
                max_height=200,
                max_bytes=0,
                quality=80,
            )
            self.assertEqual((item.width, item.height), (200, 80))  # 1000:400 = 5:2

    def test_prepare_page_alpha_fill_keeps_full_frame(self):
        """带透明通道的 PNG 转 JPEG 只铺白底，尺寸与构图不变（不裁剪）。"""
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "alpha.png"
            img = Image.new("RGBA", (300, 500), (255, 0, 0, 0))
            for x in range(50, 250):
                for y in range(100, 400):
                    img.putpixel((x, y), (0, 0, 255, 255))
            img.save(src)
            out = prepare_page(
                src,
                tmp_path / "out",
                allowed_exts={".jpg"},
                max_bytes=0,
            )
            self.assertEqual((out.width, out.height), (300, 500))
            with Image.open(out.path) as converted:
                self.assertEqual(converted.size, (300, 500))

    def test_prepare_page_auto_compresses_over_limit(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "big.png"
            import random

            random.seed(7)
            noise = Image.new("RGB", (1500, 1500))
            noise.putdata(
                [
                    (random.randrange(256), random.randrange(256), random.randrange(256))
                    for _ in range(1500 * 1500)
                ]
            )
            noise.save(src)
            self.assertGreater(src.stat().st_size, 300 * 1024)
            out = prepare_page(
                src,
                tmp_path / "out",
                allowed_exts={".png", ".jpg"},
                max_bytes=200 * 1024,
            )
            self.assertNotEqual(out.path, src)
            self.assertLessEqual(out.size_bytes, 200 * 1024)
            self.assertEqual(out.path.suffix, ".jpg")


class TestRunnerAccounts(unittest.TestCase):
    """Runner.accounts 要能回答“这次会用哪个账号发”。"""

    def test_accounts_skips_disabled_and_empty(self):
        from manga_uploader.runner import Runner
        from manga_uploader.webui import build_app

        app = build_app(
            {
                "platforms": {
                    "tieba": {"enabled": True, "cookies": {"BDUSS": "x"}, "settings": {}},
                    "bilibili": {"enabled": True, "cookies": {"SESSDATA": "y"}, "settings": {}},
                    "ehentai": {"enabled": False, "cookies": {}, "settings": {}},
                }
            }
        )
        runner = Runner(app)

        class _Stub:
            def __init__(self, text):
                self.text = text

            def identity(self):
                return self.text

        stubs = {"tieba": "hakre（uid 1）", "bilibili": "", "ehentai": "不该出现"}
        runner.make_publisher = lambda name: _Stub(stubs[name])  # type: ignore[assignment]
        self.assertEqual(runner.accounts(["tieba", "bilibili", "ehentai"]), {"tieba": "hakre（uid 1）"})

    def test_accounts_survives_probe_error(self):
        from manga_uploader.runner import Runner
        from manga_uploader.webui import build_app

        app = build_app({"platforms": {"tieba": {"enabled": True, "cookies": {"BDUSS": "x"}}}})
        runner = Runner(app)

        def _boom(name):
            raise RuntimeError("网络炸了")

        runner.make_publisher = _boom  # type: ignore[assignment]
        self.assertEqual(runner.accounts(["tieba"]), {})


class TestPerPlatformProxy(unittest.TestCase):
    """代理规则（简化后）：平台各管各的，国内默认直连、e-hentai 默认系统代理。"""

    def _publisher(self, settings: dict, common: CommonConfig):
        cfg = PlatformConfig(
            name="bilibili",
            cookies={"SESSDATA": "s", "bili_jct": "c"},
            settings=settings,
        )
        return BilibiliPublisher(cfg, common)

    def test_platform_proxy_url_is_only_a_candidate(self):
        """代理全自动：手填的地址只是候选之一，直连优先，失败能换过去。"""
        common = CommonConfig()
        pub = self._publisher({"proxy_url": "http://127.0.0.1:2222"}, common)
        self.assertEqual(pub.http.active_proxy, "")  # 国内站先直连
        self.assertIn("http://127.0.0.1:2222", [r[0] for r in pub.http._routes])

    def test_domestic_platform_is_direct_even_with_global_proxy(self):
        """国内平台默认直连：老配置里的全局代理只会变成靠后的候选。"""
        common = CommonConfig(proxy_url="http://127.0.0.1:1111", use_system_proxy=True)
        pub = self._publisher({}, common)
        self.assertEqual(pub.http.session.proxies, {})
        self.assertEqual(pub.http.active_proxy, "")
        self.assertIn("http://127.0.0.1:1111", [r[0] for r in pub.http._routes])

    def test_system_proxy_is_a_candidate_without_any_setting(self):
        """不用勾任何开关：检测到系统代理就自动作为候选（国内站排在直连之后）。"""
        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value="http://sys-proxy:7890"
        ):
            pub = self._publisher({}, CommonConfig())
            self.assertEqual(
                [r[0] for r in pub.http._routes][:2], ["", "http://sys-proxy:7890"]
            )
            pub.http._switch_route(RuntimeError("sslerror"))
            self.assertEqual(pub.http.session.proxies.get("https"), "http://sys-proxy:7890")

    def test_system_proxy_falls_back_to_global_url(self):
        """系统里没开代理时，用老配置里的全局地址当兜底路径。"""
        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value=""
        ):
            pub = self._publisher(
                {"use_system_proxy": True}, CommonConfig(proxy_url="http://127.0.0.1:1111")
            )
            self.assertEqual(
                [r[0] for r in pub.http._routes], ["", "http://127.0.0.1:1111"]
            )

    def test_auto_switches_route_when_direct_is_blocked(self):
        """直连被掐断（SSL EOF/连接重置）时自动换到代理，不用用户改设置。"""
        import requests

        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value="http://sys-proxy:7890"
        ):
            pub = self._publisher({"use_system_proxy": True}, CommonConfig())
        http = pub.http
        calls = {"n": 0}

        def fake_request(method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.text = "ok"
            resp.url = url
            return resp

        with unittest.mock.patch.object(http.session, "request", side_effect=fake_request):
            resp = http.get("https://api.xiaoheihe.cn/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 2)                      # 失败一次后立刻换路径
        self.assertEqual(http.active_proxy, "http://sys-proxy:7890")
        self.assertEqual(http.session.proxies.get("https"), "http://sys-proxy:7890")

    def test_ensure_route_locks_working_path(self):
        import requests

        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value="http://sys-proxy:7890"
        ):
            pub = self._publisher({"use_system_proxy": True}, CommonConfig())
        http = pub.http
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ConnectionError("refused")
            return unittest.mock.Mock(status_code=200, close=lambda: None)

        with unittest.mock.patch.object(http.session, "get", side_effect=fake_get):
            self.assertEqual(http.ensure_route("https://api.xiaoheihe.cn/"), "http://sys-proxy:7890")

    def test_manual_url_does_not_block_auto_switch(self):
        """手填 URL 也不会挡住自动换路：直连不通时照样能退到系统代理。"""
        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value="http://sys-proxy:7890"
        ):
            pub = self._publisher(
                {"proxy_url": "http://127.0.0.1:2222", "use_system_proxy": True},
                CommonConfig(),
            )
        routes = [r[0] for r in pub.http._routes]
        self.assertEqual(routes[:2], ["", "http://sys-proxy:7890"])
        self.assertIn("http://127.0.0.1:2222", routes)

    def test_defaults_per_platform(self):
        """默认值就是“能直接用”：国内平台直连，e-hentai 走系统代理。"""
        from manga_uploader.config import DEFAULT_SETTINGS
        from manga_uploader.publishers.ehentai import EhentaiPublisher

        def make(key: str, cls):
            cfg = PlatformConfig(
                name=key,
                cookies={"x": "1"},
                settings=dict(DEFAULT_SETTINGS.get(key, {})),
            )
            return cls(cfg, CommonConfig())

        with unittest.mock.patch.object(
            http_client, "detect_system_proxy", return_value="http://sys-proxy:7890"
        ):
            domestic = make("bilibili", BilibiliPublisher)
            self.assertEqual(domestic.http.session.proxies, {})
            overseas = make("ehentai", EhentaiPublisher)
            self.assertEqual(
                overseas.http.session.proxies.get("https"), "http://sys-proxy:7890"
            )


class TestExitIpCheck(unittest.TestCase):
    """出口 IP 检测：香港节点先提示换欧美 IP。"""

    def test_lookup_parses_different_field_names(self):
        from manga_uploader.http_client import lookup_exit_ip

        class Fake:
            def __init__(self, data):
                self.data = data

            def get_json(self, url, **_):
                if self.data is None:
                    raise RuntimeError("boom")
                return self.data

        self.assertEqual(lookup_exit_ip(Fake({"ip": "1.1.1.1", "country": "HK"})), ("1.1.1.1", "HK"))
        self.assertEqual(
            lookup_exit_ip(Fake({"ip": "2.2.2.2", "country_code": "us"})), ("2.2.2.2", "US")
        )
        with self.assertRaises(Exception):
            lookup_exit_ip(Fake(None))

    def _ehentai(self, settings: dict):
        """按真实配置构造（默认设置来自 DEFAULT_SETTINGS，不是空 dict）。"""
        from manga_uploader.config import DEFAULT_SETTINGS
        from manga_uploader.publishers.ehentai import EhentaiPublisher

        cfg = PlatformConfig(
            name="ehentai",
            cookies={"ipb_member_id": "1", "ipb_pass_hash": "h"},
            settings={**DEFAULT_SETTINGS["ehentai"], "use_system_proxy": False, **settings},
        )
        return EhentaiPublisher(cfg, CommonConfig())

    def test_hong_kong_node_is_flagged(self):
        pub = self._ehentai({})
        with unittest.mock.patch.object(pub, "exit_ip", return_value=("1.2.3.4", "HK")):
            note = pub._node_hint()
        self.assertIn("香港", note)
        self.assertIn("欧美 IP", note)
        self.assertEqual(pub._pending_note, note)

    def test_overseas_node_is_silent(self):
        pub = self._ehentai({})
        with unittest.mock.patch.object(pub, "exit_ip", return_value=("5.6.7.8", "US")):
            self.assertEqual(pub._node_hint(), "")
        self.assertEqual(pub._pending_note, "")

    def test_ip_check_can_be_disabled(self):
        pub = self._ehentai({"ip_check": False})
        with unittest.mock.patch.object(pub, "exit_ip", side_effect=AssertionError("不该查")):
            self.assertEqual(pub._node_hint(), "")

    def test_lookup_failure_does_not_break_upload(self):
        pub = self._ehentai({})
        with unittest.mock.patch.object(pub, "exit_ip", side_effect=RuntimeError("断网")):
            self.assertEqual(pub._node_hint(), "")

    def test_check_reports_hong_kong(self):
        from manga_uploader.publishers.ehentai import UPLOAD_PAGE_URL

        pub = self._ehentai({})
        resp = unittest.mock.Mock()
        resp.url = UPLOAD_PAGE_URL
        resp.text = '<form action="/manage" enctype="multipart/form-data"><input type="file" name="sfile[]"></form>'
        with unittest.mock.patch.object(pub.http, "get", return_value=resp), \
             unittest.mock.patch.object(pub, "exit_ip", return_value=("1.2.3.4", "HK")):
            result = pub.check()
        self.assertTrue(result.ok)
        self.assertIn("欧美 IP", result.message)


class TestBuildStamp(unittest.TestCase):
    def test_build_stamp_contains_version_and_revision(self):
        """界面标题要能看出跑的是哪一版代码（方便核对朋友/云端是否最新）。"""
        self.assertTrue(build_stamp().startswith(__version__))
        rev = git_revision()
        if rev:
            self.assertRegex(rev, r"^[0-9a-f]{7}$")
            self.assertEqual(build_stamp(), f"{__version__}+{rev}")

    def test_git_revision_without_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(git_revision(Path(tmp)), "")
            self.assertEqual(build_stamp(Path(tmp)), __version__)


if __name__ == "__main__":
    unittest.main()
