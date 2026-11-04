"""配置切换表（多套命名预设）后端逻辑测试，不联网。

核心约定：**每套预设是完整独立的快照**。
- 保存：整页（所有平台 + 所有 Cookie）写进预设，预设之间不共享任何字段；
- 切换：把该预设整套写进 config.yaml，绝不把上一套剩下的 Cookie 补进来；
- 保存配置 / 浏览器登录：同时更新「当前预设」，否则切走再切回来会被旧快照盖回。
"""

import tempfile
import unittest
from pathlib import Path

from manga_uploader.config import ConfigError
from manga_uploader.webui import (
    config_profiles_path,
    delete_profile_config,
    load_config_profiles,
    normalize_profile_name,
    preview_profile_config,
    rename_profile_config,
    save_config,
    save_config_with_profile,
    save_profile_config,
    set_active_profile,
    switch_profile_config,
    sync_cookies_into_active_profile,
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

    def _two_accounts(self):
        """两套账号：不做主角（BDUSS=hakre）与 茶与金平糖（BDUSS=mine）。"""
        cfg, prof = self._paths()
        cfg.write_text(
            "common: {timeout: 30}\n"
            "platforms:\n"
            "  tieba:\n"
            "    enabled: true\n"
            "    cookies: {BDUSS: hakre}\n"
            "    settings: {forum: 东方吧}\n"
            "  bilibili:\n"
            "    enabled: true\n"
            "    cookies: {SESSDATA: bili-hakre}\n"
            "    settings: {}\n",
            encoding="utf-8",
        )
        save_profile_config(
            prof,
            "不做主角",
            {
                "common": {"timeout": 30},
                "platforms": {
                    "tieba": {
                        "enabled": True,
                        "cookies": {"BDUSS": "hakre"},
                        "settings": {"forum": "东方吧"},
                    },
                    "bilibili": {
                        "enabled": True,
                        "cookies": {"SESSDATA": "bili-hakre"},
                        "settings": {},
                    },
                },
            },
        )
        save_profile_config(
            prof,
            "茶与金平糖",
            {
                "common": {"timeout": 30},
                "platforms": {
                    "tieba": {
                        "enabled": True,
                        "cookies": {"BDUSS": "mine"},
                        "settings": {"forum": "东方吧"},
                    },
                    "bilibili": {
                        "enabled": True,
                        "cookies": {"SESSDATA": "bili-mine"},
                        "settings": {},
                    },
                },
            },
        )
        return cfg, prof

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

    def test_preview_lists_written_and_cleared_cookies(self):
        """切换前能列出会被写入 / 被清空的 Cookie，防止静默换号和静默掉登录态。"""
        cfg, prof = self._paths()
        cfg.write_text(
            "common: {}\nplatforms:\n"
            "  tieba: {enabled: true, cookies: {BDUSS: mine}, settings: {}}\n"
            "  ehentai: {enabled: true, cookies: {ipb_pass_hash: h}, settings: {}}\n",
            encoding="utf-8",
        )
        other = {
            "common": {},
            "platforms": {
                "tieba": {
                    "enabled": True,
                    "cookies": {"BDUSS": "someone-else"},
                    "settings": {},
                },
                "bilibili": {
                    "enabled": True,
                    "cookies": {"SESSDATA": "s", "bili_jct": "c"},
                    "settings": {},
                },
            },
        }
        save_profile_config(prof, "朋友的号", other)
        prev = preview_profile_config(prof, cfg, "朋友的号")
        self.assertEqual(prev["sets"]["tieba"], ["BDUSS"])
        # 预设里有、config.yaml 里没有的，也算“会被写入”
        self.assertEqual(prev["sets"]["bilibili"], ["SESSDATA", "bili_jct"])
        # config.yaml 有、预设里没有的平台会被整块移除（要提醒）
        self.assertEqual(prev["drops"], ["ehentai"])
        # 预览不能改盘上的配置
        self.assertIn("BDUSS: mine", cfg.read_text(encoding="utf-8"))

        # 预设里空着的 Cookie：盘上有值就会被清掉，必须报出来
        save_profile_config(
            prof,
            "半个号",
            {
                "common": {},
                "platforms": {
                    "tieba": {"enabled": True, "cookies": {"BDUSS": ""}, "settings": {}},
                    "ehentai": {
                        "enabled": True,
                        "cookies": {"ipb_pass_hash": "h"},
                        "settings": {},
                    },
                },
            },
        )
        prev2 = preview_profile_config(prof, cfg, "半个号")
        self.assertEqual(prev2["clears"]["tieba"], ["BDUSS"])

        # 和当前值一样的预设：没有需要提醒的变更
        same = {
            "common": {},
            "platforms": {
                "tieba": {
                    "enabled": True,
                    "cookies": {"BDUSS": "mine"},
                    "settings": {},
                },
                "ehentai": {
                    "enabled": True,
                    "cookies": {"ipb_pass_hash": "h"},
                    "settings": {},
                },
            },
        }
        save_profile_config(prof, "我的号", same)
        prev3 = preview_profile_config(prof, cfg, "我的号")
        self.assertEqual(prev3["sets"], {})
        self.assertEqual(prev3["clears"], {})
        self.assertEqual(prev3["drops"], [])
        with self.assertRaises(ConfigError):
            preview_profile_config(prof, cfg, "不存在")

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

    def test_switch_is_exact_snapshot_no_cookie_leak(self):
        """回归：切到 B 不能把 A 的 Cookie 留在 config.yaml / 带进 B。

        用户实测：在「不做主角」把贴吧改成自己的 BDUSS，切到「茶与金平糖」
        还是它 —— 因为切换时把 config.yaml 的旧值“补”进了目标预设。
        """
        cfg, prof = self._two_accounts()

        switch_profile_config(prof, cfg, "不做主角")
        self.assertIn("BDUSS: hakre", cfg.read_text(encoding="utf-8"))

        got = switch_profile_config(prof, cfg, "茶与金平糖")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("BDUSS: mine", text)
        self.assertNotIn("hakre", text)  # 上一套的账号不能残留
        self.assertEqual(got["platforms"]["tieba"]["cookies"]["BDUSS"], "mine")
        self.assertEqual(
            got["platforms"]["bilibili"]["cookies"]["SESSDATA"], "bili-mine"
        )

        # 切回 A：A 的快照没被 B 污染
        back = switch_profile_config(prof, cfg, "不做主角")
        self.assertEqual(back["platforms"]["tieba"]["cookies"]["BDUSS"], "hakre")
        self.assertEqual(
            back["platforms"]["bilibili"]["cookies"]["SESSDATA"], "bili-hakre"
        )
        data = load_config_profiles(prof)
        self.assertEqual(
            data["profiles"]["茶与金平糖"]["config"]["platforms"]["tieba"]["cookies"][
                "BDUSS"
            ],
            "mine",
        )

    def test_switch_only_touches_target_profile(self):
        """切 A 只写 A 的内容：B 的预设文件内容一个字都不动。"""
        cfg, prof = self._two_accounts()
        before = load_config_profiles(prof)["profiles"]["茶与金平糖"]
        switch_profile_config(prof, cfg, "不做主角")
        after = load_config_profiles(prof)["profiles"]["茶与金平糖"]
        self.assertEqual(before["config"], after["config"])
        self.assertEqual(
            after["config"]["platforms"]["tieba"]["cookies"]["BDUSS"], "mine"
        )

    def test_save_config_syncs_active_profile(self):
        """「保存配置」要同时更新当前预设，否则切走再切回来会变回旧号。"""
        cfg, prof = self._two_accounts()
        switch_profile_config(prof, cfg, "茶与金平糖")
        mine = {
            "common": {"timeout": 30},
            "platforms": {
                "tieba": {
                    "enabled": True,
                    "cookies": {"BDUSS": "mine-v2"},
                    "settings": {},
                }
            },
        }
        result = save_config_with_profile(cfg, prof, mine)
        self.assertEqual(result["profile"], "茶与金平糖")
        self.assertIn("BDUSS: mine-v2", cfg.read_text(encoding="utf-8"))
        # 来回切一次仍然是我刚保存的那份
        switch_profile_config(prof, cfg, "不做主角")
        got = switch_profile_config(prof, cfg, "茶与金平糖")
        self.assertEqual(got["platforms"]["tieba"]["cookies"]["BDUSS"], "mine-v2")
        # 其它预设不受影响
        self.assertEqual(
            load_config_profiles(prof)["profiles"]["不做主角"]["config"]["platforms"][
                "tieba"
            ]["cookies"]["BDUSS"],
            "hakre",
        )

    def test_save_config_without_active_profile_does_not_create_profiles_file(self):
        cfg, prof = self._paths()
        mine = {
            "common": {},
            "platforms": {
                "tieba": {"enabled": True, "cookies": {"BDUSS": "x"}, "settings": {}}
            },
        }
        result = save_config_with_profile(cfg, prof, mine)
        self.assertEqual(result["profile"], "")
        self.assertIn("BDUSS: x", cfg.read_text(encoding="utf-8"))
        self.assertFalse(prof.exists())

    def test_browser_login_cookies_sync_into_active_profile(self):
        """浏览器登录拿到的 Cookie 也要进当前预设，否则切一次就回到旧号。"""
        cfg, prof = self._two_accounts()
        switch_profile_config(prof, cfg, "茶与金平糖")
        # 浏览器登录的落盘顺序：先 update_platform_cookies 写 config.yaml，
        # 再 sync_cookies_into_active_profile 写当前预设
        save_config(cfg, {"common": {"timeout": 30}, "platforms": {
            "tieba": {"enabled": True, "cookies": {"BDUSS": "fresh"}, "settings": {}},
            "bilibili": {"enabled": True, "cookies": {"SESSDATA": "bili-mine"}, "settings": {}},
        }})
        synced = sync_cookies_into_active_profile(prof, "tieba", {"BDUSS": "fresh"})
        self.assertEqual(synced, "茶与金平糖")
        got = switch_profile_config(prof, cfg, "不做主角")
        self.assertEqual(got["platforms"]["tieba"]["cookies"]["BDUSS"], "hakre")
        got = switch_profile_config(prof, cfg, "茶与金平糖")
        self.assertEqual(got["platforms"]["tieba"]["cookies"]["BDUSS"], "fresh")

    def test_sync_cookies_without_active_profile_is_noop(self):
        """没有「当前预设」时只写 config.yaml，不去动预设文件。"""
        cfg, prof = self._paths()
        save_profile_config(
            prof,
            "不管",
            {
                "common": {},
                "platforms": {
                    "tieba": {"enabled": True, "cookies": {"BDUSS": "x"}, "settings": {}}
                },
            },
        )
        self.assertEqual(sync_cookies_into_active_profile(prof, "tieba", {"BDUSS": "y"}), "")
        self.assertFalse(prof.exists() and load_config_profiles(prof)["active"])

    def test_set_active_profile(self):
        cfg, prof = self._two_accounts()
        set_active_profile(prof, "茶与金平糖")
        self.assertEqual(load_config_profiles(prof)["active"], "茶与金平糖")
        # set_active_profile 不写 config.yaml
        self.assertIn("BDUSS: hakre", cfg.read_text(encoding="utf-8"))
        with self.assertRaises(ConfigError):
            set_active_profile(prof, "不存在")

    def test_save_rejects_empty_payload(self):
        _, prof = self._paths()
        with self.assertRaises(ConfigError):
            save_profile_config(prof, "空的", {"common": {}, "platforms": {}})


if __name__ == "__main__":
    unittest.main()
