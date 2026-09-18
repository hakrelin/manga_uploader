"""本地覆盖更新器的规划/保护/同步逻辑测试（不联网）。"""

import tempfile
import unittest
import zipfile
import http.client
from pathlib import Path, PurePosixPath
from unittest import mock

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


class DownloadFallbackTest(unittest.TestCase):
    """GitHub 在国内常被重置连接：要给出人话提示，并自动试内置镜像。"""

    def test_candidates_try_direct_then_mirrors(self):
        cands = updater.download_candidates(
            "https://github.com/a/b", ["main", "master"]
        )
        self.assertEqual(cands[0], ("main", "https://github.com/a/b/archive/refs/heads/main.zip", "直连"))
        self.assertEqual(cands[1][0], "main")
        self.assertTrue(cands[1][1].startswith(updater.MIRROR_PREFIXES[0]))
        self.assertTrue(cands[1][1].endswith("/https://github.com/a/b/archive/refs/heads/main.zip"))
        # main 的直连+镜像试完才轮到 master
        master = [c for c in cands if c[0] == "master"]
        self.assertEqual(master[0][1], "https://github.com/a/b/archive/refs/heads/master.zip")
        self.assertGreater(len(cands), len(master))

    def test_candidates_can_skip_mirrors(self):
        cands = updater.download_candidates(
            "https://github.com/a/b", ["main"], no_mirror=True
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0][2], "直连")

    def test_download_zip_fails_fast_on_4xx(self):
        """404/403 属于确定性错误：不要傻等 3 轮重试。"""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "x.zip"
            calls = {"n": 0}

            def not_found(*_args, **_kwargs):
                calls["n"] += 1
                resp = mock.Mock()
                resp.status_code = 404
                err = RuntimeError("404 Client Error")
                err.response = resp  # type: ignore[attr-defined]
                raise err

            with mock.patch.object(updater, "_download_once", side_effect=not_found), \
                    mock.patch.object(updater.time, "sleep", lambda *_a: None):
                with self.assertRaises(updater.UpdaterError) as ctx:
                    updater.download_zip("https://github.com/a/b/archive.zip", dest)
            self.assertEqual(calls["n"], 1, "4xx 不应该重试")
            self.assertIn("HTTP 404", str(ctx.exception))

    def test_download_zip_wraps_connection_reset(self):
        """远端直接断开（RemoteDisconnected）不能把 traceback 甩给用户。"""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "x.zip"

            def boom(*_args, **_kwargs):
                raise http.client.RemoteDisconnected(
                    "Remote end closed connection without response"
                )

            with mock.patch.object(updater, "_download_once", side_effect=boom), \
                    mock.patch.object(updater.time, "sleep", lambda *_a: None):
                with self.assertRaises(updater.UpdaterError) as ctx:
                    updater.download_zip("https://github.com/a/b/archive.zip", dest)
            text = str(ctx.exception)
            self.assertIn("下载失败", text)
            self.assertIn("RemoteDisconnected", text)
            self.assertIn("代理", text)          # 告诉用户怎么办
            self.assertIn("--url", text)
            self.assertIn(updater.MIRROR_PREFIXES[0], text)


class ScriptEncodingTest(unittest.TestCase):
    """Windows PowerShell 5.1 靠 BOM 认 UTF-8：脚本缺 BOM 会被按 GBK 解析而报错。"""

    def test_adds_bom_to_scripts_missing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "update.ps1"
            plain.write_bytes("# 更新脚本\nRead-Host \"按回车\"\n".encode("utf-8"))
            already = root / "start-web.ps1"
            already.write_bytes(b"\xef\xbb\xbf# ok\n")
            (root / "not-a-script.txt").write_text("x", encoding="utf-8")

            fixed = updater.ensure_script_encodings(root)

            self.assertEqual(fixed, ["update.ps1"])
            self.assertTrue(plain.read_bytes().startswith(b"\xef\xbb\xbf"))
            # 已有 BOM 的不重复加
            self.assertEqual(already.read_bytes().count(b"\xef\xbb\xbf"), 1)
            # 无关文件不动
            self.assertFalse(
                (root / "not-a-script.txt").read_bytes().startswith(b"\xef\xbb\xbf")
            )

    def test_missing_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(updater.ensure_script_encodings(Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
