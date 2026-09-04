"""_materialize_edits（发布/检查点的全体副本物化）单元测试。"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from manga_uploader.web import _materialize_edits
from manga_uploader.comic import load_chapters


def _make_comic(root: Path, names: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        Image.new("RGB", (10, 10), (100, 80, 160)).save(root / n)
    (root / "manga.json").write_text(
        '{"title": "T", "chapters": [{"folder": "root"}]}', encoding="utf-8"
    )


def _png_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (10, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


class TestMaterializeEdits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comic = self.root / "comic"
        _make_comic(self.comic, ["001.jpg", "002.jpg", "003.jpg"])

    def tearDown(self):
        self.tmp.cleanup()

    def _pages(self):
        ch = load_chapters(self.comic, strict=False)[0]
        return [p.name for p in ch.pages]

    def _files(self):
        return sorted(p.name for p in self.comic.iterdir() if p.suffix in (".jpg", ".png"))

    def test_keep_and_reorder(self):
        _materialize_edits(str(self.comic), {"root": {"pages": [
            {"keep": "003.jpg"}, {"keep": "001.jpg"}, {"keep": "002.jpg"},
        ]}}, {})
        self.assertEqual(self._pages(), ["003.jpg", "001.jpg", "002.jpg"])
        self.assertEqual(self._files(), ["001.jpg", "002.jpg", "003.jpg"])

    def test_swap_rename_two_phase(self):
        """002→001 / 001→002 交换：两阶段重命名不得互相覆盖。"""
        _materialize_edits(str(self.comic), {"root": {"pages": [
            {"rename": "001.jpg", "from": "002.jpg"},
            {"rename": "002.jpg", "from": "001.jpg"},
            {"keep": "003.jpg"},
        ]}}, {})
        sizes = {n: (self.comic / n).stat().st_size for n in ("001.jpg", "002.jpg")}
        # 两张测试图同尺寸同色——用内容哈希之外的顺序验证：页序按清单走即可
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertEqual(len(sizes), 2)

    def test_set_and_diff_delete(self):
        """set 新图 + 缺席清单的磁盘页按差集删除。"""
        _materialize_edits(str(self.comic), {"root": {"pages": [
            {"keep": "001.jpg"},
            {"set": "newpic.png", "upload": "u0"},
        ]}}, {"u0": _png_bytes()})
        self.assertEqual(self._pages(), ["001.jpg", "newpic.png"])
        self.assertEqual(self._files(), ["001.jpg", "newpic.png"])

    def test_replace_same_name_overwrites(self):
        data = _png_bytes()
        _materialize_edits(str(self.comic), {"root": {"pages": [
            {"set": "001.jpg", "upload": "u0"},
            {"keep": "002.jpg"},
            {"keep": "003.jpg"},
        ]}}, {"u0": data})
        self.assertEqual((self.comic / "001.jpg").read_bytes(), data)
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])

    def test_rename_to_missing_target_conflict(self):
        """rename 目标被不参与改名的磁盘页占着 → 报错且磁盘零损伤。"""
        with self.assertRaises(ValueError):
            _materialize_edits(str(self.comic), {"root": {"pages": [
                {"rename": "002.jpg", "from": "001.jpg"},
                {"keep": "002.jpg"},
                {"keep": "003.jpg"},
            ]}}, {})
        self.assertEqual(self._files(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])

    def test_delete_mid_page_then_renumber(self):
        """删掉中间页后再整体重命名：002 消失、003→002 应成功（目标是被删页）。

        前端「删除第 2 页 → 全部按页序重命名」就是这条路径；旧实现按改名前的
        磁盘快照误判“目标 002.jpg 已存在”，导致发布失败。
        """
        extra = self.root / "extra"
        _make_comic(extra, ["001.jpg", "002.jpg", "003.jpg", "004.jpg"])
        self.comic = extra
        _materialize_edits(str(self.comic), {"root": {"pages": [
            {"keep": "001.jpg"},
            {"rename": "002.jpg", "from": "003.jpg"},
            {"rename": "003.jpg", "from": "004.jpg"},
        ]}}, {})
        self.assertEqual(self._files(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])

    def test_missing_upload_rolls_back(self):
        """上传引用缺失 → 报错，重命名回滚，不留临时文件。"""
        with self.assertRaises(ValueError):
            _materialize_edits(str(self.comic), {"root": {"pages": [
                {"rename": "009.jpg", "from": "001.jpg"},
                {"set": "new.png", "upload": "ghost"},
            ]}}, {})
        self.assertEqual(self._files(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertFalse(list(self.comic.glob(".mu_tmp_*")))

    def test_duplicate_final_name_rejected_before_mutation(self):
        """最终页名重复（两个 set/rename 指向同一名）在改文件前拒绝，零损伤。"""
        with self.assertRaises(ValueError):
            _materialize_edits(str(self.comic), {"root": {"pages": [
                {"set": "001.jpg", "upload": "a"},
                {"set": "001.jpg", "upload": "b"},
            ]}}, {"a": _png_bytes(), "b": _png_bytes()})
        self.assertEqual(self._files(), ["001.jpg", "002.jpg", "003.jpg"])
        self.assertFalse(list(self.comic.glob(".mu_tmp_*")))

    def test_empty_edits_noop(self):
        _materialize_edits(str(self.comic), None, {})
        _materialize_edits(str(self.comic), {}, {})
        self.assertEqual(self._pages(), ["001.jpg", "002.jpg", "003.jpg"])


if __name__ == "__main__":
    unittest.main()
