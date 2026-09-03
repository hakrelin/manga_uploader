import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image

from manga_uploader.comic import load_chapters, platform_meta
from manga_uploader.comic import page_sequence_warnings
from manga_uploader.config import load_config, missing_cookies
from manga_uploader.publishers.ehentai import _parse_upload_page
from manga_uploader.publishers.tieba import _find_first
from manga_uploader.http_client import _clean_proxy_url, detect_system_proxy
from manga_uploader.util import prepare_page


class TestComicScan(unittest.TestCase):
    def setUp(self):
        self.demo = Path("examples/my_comic")
        self.tmp = tempfile.TemporaryDirectory()

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
    def test_load_example(self):
        cfg = load_config("config.example.yaml")
        self.assertIn("bilibili", cfg.platforms)
        self.assertIn("zaimanhua", cfg.platforms)
        self.assertTrue(cfg.platforms["bilibili"].enabled)
        self.assertEqual(cfg.platforms["bilibili"].get("max_pages_per_post"), 9)
        self.assertEqual(cfg.platforms["bilibili"].get("publish_mode"), "article")
        self.assertAlmostEqual(cfg.common.max_bytes_mb, 10.0)
        self.assertFalse(cfg.platforms["bilibili"].get("use_system_proxy"))
        self.assertFalse(cfg.platforms["zaimanhua"].get("use_system_proxy"))
        # e-hentai 单独配置为走代理（海外站）
        self.assertTrue(cfg.platforms["ehentai"].get("use_system_proxy"))
        self.assertEqual(
            cfg.platforms["ehentai"].get("proxy_url"), "http://127.0.0.1:7897"
        )
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


if __name__ == "__main__":
    unittest.main()
