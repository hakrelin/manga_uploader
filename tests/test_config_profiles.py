"""配置切换表（多套命名预设）后端逻辑测试，不联网。"""

import tempfile
import unittest
from pathlib import Path

from manga_uploader.config import ConfigError
from manga_uploader.webui import (
    config_profiles_path,
    delete_profile_config,
    load_config_profiles,
    normalize_profile_name,
    rename_profile_config,
    save_profile_config,
    switch_profile_config,
)


class ConfigProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _paths(self):
        cfg = self.base / "config.yaml"
        cfg.write_text(
            "ai: {enabled: true, model: m}\ncommon: {}\nplatforms: {}\n",
            encoding="utf-8",
        )
        return cfg, config_profiles_path(cfg)

    @staticmethod
    def _payload():
        return {
            "common": {"timeout": 12, "proxy_url": ""},
            "platforms": {
                "bilibili": {
                    "enabled": True,
                    "cookies": {"SESSDATA": "s", "bili_jct": "c"},
                    "settings": {"publish_mode": "article"},
                }
            },
        }

    def test_full_crud_and_switch_keeps_ai(self):
        cfg, prof = self._paths()
        created, _ = save_profile_config(prof, "我的汉化组", self._payload())
        self.assertTrue(created)
        self.assertEqual(
            load_config_profiles(prof)["profiles"]["我的汉化组"]["config"]["common"][
                "timeout"
            ],
            12,
        )

        # 覆盖保存不算新建
        created2, _ = save_profile_config(prof, "我的汉化组", self._payload())
        self.assertFalse(created2)

        # 重命名（当前指向会跟随）
        switch_profile_config(prof, cfg, "我的汉化组")
        result = rename_profile_config(prof, "我的汉化组", "主力发布号")
        self.assertEqual(result["active"], "主力发布号")

        # 切换会写 config.yaml，且保留文件里的 ai 段
        got = switch_profile_config(prof, cfg, "主力发布号")
        self.assertEqual(got["common"]["timeout"], 12)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("ai:", text)
        self.assertIn("enabled: true", text)

        # 删除后 active 清空
        self.assertTrue(delete_profile_config(prof, "主力发布号"))
        data = load_config_profiles(prof)
        self.assertEqual(data["active"], "")
        self.assertEqual(data["profiles"], {})

    def test_rename_conflict_and_missing(self):
        _, prof = self._paths()
        save_profile_config(prof, "A", self._payload())
        save_profile_config(prof, "B", self._payload())
        with self.assertRaises(ConfigError):
            rename_profile_config(prof, "A", "B")
        with self.assertRaises(ConfigError):
            switch_profile_config(prof, prof, "不存在")

    def test_name_validation(self):
        with self.assertRaises(ConfigError):
            normalize_profile_name("   ")
        with self.assertRaises(ConfigError):
            normalize_profile_name("x" * 51)
        self.assertEqual(normalize_profile_name("  主力号 "), "主力号")

    def test_profiles_file_sibling(self):
        cfg, prof = self._paths()
        self.assertEqual(prof.name, "config.profiles.yaml")
        self.assertEqual(prof.parent, cfg.parent)


if __name__ == "__main__":
    unittest.main()
