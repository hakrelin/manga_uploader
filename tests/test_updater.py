"""本地覆盖更新器的规划/保护/同步逻辑测试（不联网）。"""

import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath

from manga_uploader import updater


class ProtectTest(unittest.TestCase):
    def test_protected_dirs_and_files(self):
        self.assertTrue(updater._is_protected(PurePosixPath(".venv/Scripts/python.exe")))
        self.assertTrue(updater._is_protected(PurePosixPath(".tools/python/python.exe")))
        self.assertTrue(updater._is_protected(PurePosixPath(".git/config")))
        self.assertTrue(updater._is_protected(PurePosixPath("output/report.json")))
        self.assertTrue(updater._is_protected(PurePosixPath("config.yaml")))
        self.assertTrue(updater._is_protected(PurePosixPath("config.local.yaml")))
        self.assertTrue(
            updater._is_protected(PurePosixPath(updater.STATE_NAME))
        )
        self.assertFalse(
            updater._is_protected(PurePosixPath("manga_uploader/publishers/bilibili.py"))
        )
        self.assertFalse(updater._is_protected(PurePosixPath("README.md")))


class SyncPlanTest(unittest.TestCase):
    def test_first_run_only_overwrite_no_delete(self):
        to_delete, to_overwrite = updater.sync_plan(None, ["a.py", "b.txt"])
        self.assertEqual(to_delete, [])
        self.assertEqual(to_overwrite, ["a.py", "b.txt"])

    def test_stale_files_removed_only_when_recorded(self):
        to_delete, to_overwrite = updater.sync_plan(
            ["old.py", "keep.py"], ["keep.py", "new.py"]
        )
        self.assertEqual(to_delete, ["old.py"])
        self.assertEqual(to_overwrite, ["keep.py", "new.py"])

    def test_protected_paths_never_deleted(self):
        to_delete, _ = updater.sync_plan(
            ["config.yaml", ".venv/x", "output/old.json", "keep.py"],
            ["keep.py"],
        )
        self.assertEqual(to_delete, [])

    def test_user_untracked_files_not_deleted(self):
        # 从未进入 manifest 的本机文件（如 local 字体/用户目录）不会被同步删除
        to_delete2, _ = updater.sync_plan(
            ["manga_uploader/web/assets/fonts/local-a.ttf", "code.py"], ["code2.py"]
        )
        self.assertEqual(to_delete2, ["code.py"])


class ExtractTest(unittest.TestCase):
    def _make_zip(self, path: Path, entries: dict[str, bytes]):
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)

    def test_extract_strips_github_top_dir(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "src.zip"
            self._make_zip(
                zip_path,
                {
                    "manga_uploader-main/manga_uploader/__init__.py": b"v",
                    "manga_uploader-main/README.md": b"readme",
                },
            )
            dest = base / "new"
            dest.mkdir()
            updater._extract_zip(zip_path, dest)
            self.assertTrue((dest / "manga_uploader/__init__.py").is_file())
            self.assertTrue((dest / "README.md").is_file())
            self.assertFalse((dest / "manga_uploader-main").exists())

    def test_extract_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            zip_path = base / "evil.zip"
            self._make_zip(
                zip_path,
                {"repo-top/../escape.txt": b"bad"},
            )
            dest = base / "new"
            dest.mkdir()
            with self.assertRaises(updater.UpdaterError):
                updater._extract_zip(zip_path, dest)


class ApplyTest(unittest.TestCase):
    def test_apply_overwrites_removes_stale_keeps_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "manga_uploader").mkdir(parents=True)
            (root / "manga_uploader" / "old.py").write_text("old", encoding="utf-8")
            (root / "config.yaml").write_text("secret", encoding="utf-8")
            old_files = ["manga_uploader/old.py"]

            new_root = root / "new_version"
            (new_root / "manga_uploader").mkdir(parents=True)
            (new_root / "manga_uploader" / "new.py").write_text("new", encoding="utf-8")
            (new_root / "README.md").write_text("r", encoding="utf-8")

            updater.apply_update(root, new_root, old_files)

            self.assertFalse((root / "manga_uploader" / "old.py").exists())
            self.assertTrue((root / "manga_uploader" / "new.py").is_file())
            self.assertTrue((root / "README.md").is_file())
            self.assertEqual(
                (root / "config.yaml").read_text(encoding="utf-8"), "secret"
            )


if __name__ == "__main__":
    unittest.main()
