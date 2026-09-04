"""staff 页命名（封面名+staff，如 001staff.png）与重复生成覆盖行为测试。"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from manga_uploader.comic import load_chapters
from manga_uploader.webui import (
    is_staff_page_name,
    read_staff_rows,
    staff_page_name,
    upsert_staff_page,
    write_staff_rows,
)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 30, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _make_comic(root: Path, names: list[str], pages=None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data = _png_bytes()
    for n in names:
        (root / n).write_bytes(data)
    payload = {"title": "T", "chapters": [{"folder": "root"}]}
    if pages is not None:
        payload["chapters"][0]["pages"] = list(pages)
    (root / "manga.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _pages(root: Path) -> list[str]:
    chapter = load_chapters(root, strict=False)[0]
    return [p.name for p in chapter.pages]


class TestStaffPageName(unittest.TestCase):
    def test_name_format(self):
        self.assertEqual(staff_page_name("001.jpg"), "001staff.png")
        self.assertEqual(staff_page_name("cover.PNG"), "coverstaff.png")
        self.assertEqual(staff_page_name("p01.v2.jpeg"), "p01.v2staff.png")

    def test_staff_name_detection(self):
        self.assertTrue(is_staff_page_name("staff.png"))
        self.assertTrue(is_staff_page_name("staff.jpg"))
        self.assertTrue(is_staff_page_name("001staff.png"))
        self.assertFalse(is_staff_page_name("001.jpg"))
        self.assertFalse(is_staff_page_name("001.jpg.png"))


class TestUpsertStaffPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "comic"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_generation_slots_after_cover(self):
        _make_comic(self.root, ["001.jpg", "002.jpg", "003.jpg"])
        count = upsert_staff_page(self.root, "root", _png_bytes())
        self.assertEqual(count, 4)
        self.assertEqual(
            _pages(self.root),
            ["001.jpg", "001staff.png", "002.jpg", "003.jpg"],
        )
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertEqual(files, ["001.jpg", "001staff.png", "002.jpg", "003.jpg"])

    def test_regeneration_does_not_stack(self):
        _make_comic(self.root, ["001.jpg", "002.jpg", "003.jpg"])
        upsert_staff_page(self.root, "root", _png_bytes())
        upsert_staff_page(self.root, "root", _png_bytes())
        upsert_staff_page(self.root, "root", _png_bytes())
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertEqual(files, ["001.jpg", "001staff.png", "002.jpg", "003.jpg"])

    def test_legacy_fixed_staff_replaced(self):
        _make_comic(
            self.root,
            ["001.jpg", "002.jpg", "003.jpg", "staff.png"],
            pages=["001.jpg", "staff.png", "002.jpg", "003.jpg"],
        )
        upsert_staff_page(self.root, "root", _png_bytes())
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertEqual(files, ["001.jpg", "001staff.png", "002.jpg", "003.jpg"])
        self.assertEqual(
            _pages(self.root),
            ["001.jpg", "001staff.png", "002.jpg", "003.jpg"],
        )

    def test_renamed_cover_replaces_old_dynamic_name(self):
        _make_comic(self.root, ["001.jpg", "002.jpg", "003.jpg"])
        upsert_staff_page(self.root, "root", _png_bytes())
        # 封面改名后旧 staff 名（001staff.png）也要被替换成新封面派生名
        (self.root / "001.jpg").replace(self.root / "000.jpg")
        meta = json.loads((self.root / "manga.json").read_text(encoding="utf-8"))
        meta["chapters"][0]["pages"] = [
            "000.jpg", "001staff.png", "002.jpg", "003.jpg",
        ]
        (self.root / "manga.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        upsert_staff_page(self.root, "root", _png_bytes())
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertEqual(files, ["000.jpg", "000staff.png", "002.jpg", "003.jpg"])
        self.assertEqual(
            _pages(self.root),
            ["000.jpg", "000staff.png", "002.jpg", "003.jpg"],
        )

    def test_001_rename_keeps_staff_identity(self):
        """001 重命名后 staff 页应保留为“封面名+staff”，而不是被当普通页编号。"""
        from manga_uploader.web import _materialize_edits

        names = ["001.jpg", "002.jpg", "003.jpg", "004.jpg", "005.jpg", "staff.png"]
        _make_comic(
            self.root,
            names,
            pages=["001.jpg", "staff.png", "002.jpg", "003.jpg", "004.jpg", "005.jpg"],
        )
        # 与前端 001 重命名一致：普通页继续编号，staff 页保留 staff 后缀
        items = [
            {"keep": "001.jpg"},
            {"rename": "001staff.png", "from": "staff.png"},
            {"keep": "002.jpg"},
            {"keep": "003.jpg"},
            {"keep": "004.jpg"},
            {"keep": "005.jpg"},
        ]
        _materialize_edits(self.root, {"root": {"pages": items}}, {})
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertEqual(
            files,
            ["001.jpg", "001staff.png", "002.jpg", "003.jpg", "004.jpg", "005.jpg"],
        )
        self.assertEqual(
            _pages(self.root),
            ["001.jpg", "001staff.png", "002.jpg", "003.jpg", "004.jpg", "005.jpg"],
        )

    def test_empty_staff_rows_stay_empty(self):
        """清空名单后重开不应又被默认职位模版顶回来。"""
        _make_comic(self.root, ["001.jpg", "002.jpg"])
        write_staff_rows(self.root, "root", [], bg=0)
        saved = read_staff_rows(self.root, "root")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["rows"], [])
        self.assertEqual(saved["bg"], 0)

    def test_regeneration_keeps_user_owned_staff_like_page(self):
        """只清理工具自产 staff 页，不删除用户自己命名/加入的 xxxstaff 图。"""
        _make_comic(self.root, ["001.jpg", "002.jpg", "003.jpg", "mystaff.png"])
        before_files = sorted(p.name for p in self.root.iterdir() if p.suffix == ".png")
        self.assertIn("mystaff.png", before_files)
        upsert_staff_page(self.root, "root", _png_bytes())
        files = sorted(p.name for p in self.root.iterdir() if p.suffix in (".jpg", ".png"))
        self.assertIn("mystaff.png", files)
        self.assertIn("001staff.png", files)


if __name__ == "__main__":
    unittest.main()
