"""GUI 帮助函数测试（Cookie 解析、漫画导入辅助，不创建窗口）。"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from manga_uploader.gui import (
    UploaderApp,
    format_full_preview,
    join_cookie_text,
    parse_cookie_text,
)
from manga_uploader.models import Chapter


class TestCookieText(unittest.TestCase):
    def test_parse_semicolon(self):
        text = 'SESSDATA=abc123; bili_jct=csrf_token; buvid3="quoted"'
        self.assertEqual(
            parse_cookie_text(text),
            {"SESSDATA": "abc123", "bili_jct": "csrf_token", "buvid3": "quoted"},
        )

    def test_parse_json(self):
        self.assertEqual(
            parse_cookie_text('{"BDUSS": "x"}'), {"BDUSS": "x"}
        )

    def test_parse_ignores_empty(self):
        self.assertEqual(parse_cookie_text(" ; ;  "), {})

    def test_join_round_trip(self):
        cookies = {"token": "jwt", "clientId": "c1"}
        self.assertEqual(parse_cookie_text(join_cookie_text(cookies)), cookies)


class TestImportHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _png(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 40), "red").save(path)

    def test_stage_images_numbered_by_natural_order(self):
        src = self.root / "src"
        self._png(src / "page2.png")
        self._png(src / "page10.png")
        self._png(src / "page1.png")
        images = sorted(src.iterdir())
        staged = UploaderApp._stage_images(images, title_hint="测试本")
        try:
            names = sorted(p.name for p in staged.iterdir())
            self.assertEqual(names, ["001.png", "002.png", "003.png"])
        finally:
            import shutil

            shutil.rmtree(staged, ignore_errors=True)

    def test_write_quick_meta(self):
        folder = self.root / "meta"
        folder.mkdir()
        UploaderApp._write_quick_meta(
            folder, {"title": "我的漫画", "author": "阿明", "description": "简介"}
        )
        data = json.loads((folder / "manga.json").read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "我的漫画")
        self.assertEqual(data["chapters"][0]["folder"], "root")
        self.assertEqual(data["chapters"][0]["title"], "我的漫画")

    def test_looks_like_full_comic(self):
        # 多话：子目录各放图片
        multi = self.root / "multi"
        self._png(multi / "ch01" / "1.png")
        self._png(multi / "ch02" / "1.png")
        self.assertTrue(UploaderApp._looks_like_full_comic(multi))
        # 单本：根目录直接放图
        flat = self.root / "flat"
        self._png(flat / "1.png")
        self.assertFalse(UploaderApp._looks_like_full_comic(flat))
        # 带 manga.json 的单本也算完整目录
        with_meta = self.root / "meta"
        self._png(with_meta / "1.png")
        (with_meta / "manga.json").write_text("{}", encoding="utf-8")
        self.assertTrue(UploaderApp._looks_like_full_comic(with_meta))

    def test_unwrap_single_dir(self):
        outer = self.root / "outer"
        inner = outer / "inner" / "deeper"
        self._png(inner / "1.png")
        self.assertEqual(
            UploaderApp._unwrap_single_dir(outer),
            inner,
        )

    def test_extract_zip_rejects_traversal(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../evil.txt", "x")
        with self.assertRaises(ValueError):
            UploaderApp._extract_zip(archive, self.root / "dest")

    def test_extract_zip_ok(self):
        archive = self.root / "ok.cbz"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ch01/001.jpg", b"data")
            zf.writestr("ch01/002.jpg", b"data")
        dest = UploaderApp._extract_zip(archive, self.root / "dest")
        self.assertTrue((dest / "ch01" / "001.jpg").exists())


class TestFullPreviewFormat(unittest.TestCase):
    def test_format_contains_chapter_and_platform(self):
        chapter = Chapter(
            key="ch01",
            title="预览标题",
            description="",
            pages=[],
        )
        preview = [(chapter, [("bilibili", ["发布平台：B站（专栏文章）", "[1] 001.png"])])]
        text = format_full_preview(preview)
        self.assertIn("预览标题", text)
        self.assertIn("● bilibili", text)
        self.assertIn("001.png", text)


if __name__ == "__main__":
    unittest.main()
