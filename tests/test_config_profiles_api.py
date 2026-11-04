"""配置预设的 HTTP 接口回归测试（两套账号互相串号的现场复现）。

用户反馈的现场：
1. 「不做主角」和「茶与金平糖」各存了一套，但改其中一套的贴吧 Cookie，
   另一套也跟着变；
2. 在「不做主角」把 BDUSS 改成自己的、点保存，来回切一次又变回原来的。

根因是切换时的“合并补值” + 页面合并渲染：两套预设的 Cookie 互相渗透。
这里从 HTTP 层把这两条钉住。
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from manga_uploader import web


HAKRE = "hakre-bduss"
MINE = "mine-bduss"


class ConfigProfilesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = self.dir / "config.yaml"
        self.cfg.write_text(
            "common:\n  timeout: 30\n"
            "platforms:\n"
            "  tieba:\n    enabled: true\n    cookies:\n      BDUSS: " + HAKRE + "\n"
            "    settings:\n      forum: 东方吧\n"
            "  bilibili:\n    enabled: true\n    cookies:\n      SESSDATA: bili-hakre\n"
            "    settings: {}\n",
            encoding="utf-8",
        )
        self.state = web.ServerState(config_path=str(self.cfg))
        self.server = web.MangaServer(("127.0.0.1", 0), self.state)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- 工具 --

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.server.csrf_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _state_config(self) -> dict:
        return self._get("/api/state")["config"]

    def _bduss(self) -> str:
        return (
            self._state_config()["platforms"]["tieba"]["cookies"].get("BDUSS", "")
        )

    def _payload(self, bduss: str, bili: str) -> dict:
        return {
            "common": {"timeout": 30},
            "platforms": {
                "tieba": {
                    "enabled": True,
                    "cookies": {"BDUSS": bduss},
                    "settings": {"forum": "东方吧"},
                },
                "bilibili": {
                    "enabled": True,
                    "cookies": {"SESSDATA": bili},
                    "settings": {},
                },
            },
        }

    def _save_profile(self, name: str, bduss: str, bili: str, apply: bool = True) -> dict:
        return self._post(
            "/api/config-profiles",
            {
                "action": "save",
                "name": name,
                "config": self._payload(bduss, bili),
                "apply": apply,
            },
        )

    # -- 用例 --

    def test_switch_does_not_mix_accounts(self):
        """两套预设各管各的：切谁就是谁，绝不把另一套的 BDUSS 留下来。"""
        r1 = self._save_profile("不做主角", HAKRE, "bili-hakre")
        self.assertTrue(r1["applied"])
        self.assertEqual(self._bduss(), HAKRE)

        r2 = self._save_profile("茶与金平糖", MINE, "bili-mine")
        self.assertEqual(r2["active"], "茶与金平糖")
        self.assertEqual(self._bduss(), MINE)

        got = self._post("/api/config-profiles", {"action": "switch", "name": "不做主角"})
        self.assertEqual(got["config"]["platforms"]["tieba"]["cookies"]["BDUSS"], HAKRE)
        self.assertEqual(self._bduss(), HAKRE)

        got = self._post("/api/config-profiles", {"action": "switch", "name": "茶与金平糖"})
        self.assertEqual(self._bduss(), MINE)
        # 两套预设文件里各自的号都还原样留着（切换只写 config.yaml）
        from manga_uploader.webui import config_profiles_path, load_config_profiles

        profiles = load_config_profiles(config_profiles_path(self.cfg))["profiles"]
        self.assertEqual(
            profiles["不做主角"]["config"]["platforms"]["tieba"]["cookies"]["BDUSS"],
            HAKRE,
        )
        self.assertEqual(
            profiles["茶与金平糖"]["config"]["platforms"]["tieba"]["cookies"]["BDUSS"],
            MINE,
        )

    def test_save_config_updates_active_profile(self):
        """「保存配置」要连当前预设一起更新，否则切走再回来就被旧快照盖回去。"""
        self._save_profile("不做主角", HAKRE, "bili-hakre")
        # 用户把贴吧 Cookie 改成自己的，点「保存配置」
        body = {"config": self._payload(MINE, "bili-hakre")}
        saved = self._post("/api/config", body)
        self.assertEqual(saved.get("profile"), "不做主角")
        self.assertEqual(self._bduss(), MINE)

        # 来回切一次仍然是自己的号
        self._save_profile("茶与金平糖", "other-bduss", "bili-other")
        self._post("/api/config-profiles", {"action": "switch", "name": "不做主角"})
        self.assertEqual(self._bduss(), MINE)

    def test_preview_reports_what_will_be_written_and_cleared(self):
        self._save_profile("不做主角", HAKRE, "bili-hakre")
        self._save_profile("茶与金平糖", MINE, "bili-mine")
        prev = self._post(
            "/api/config-profiles", {"action": "preview", "name": "不做主角"}
        )
        # 当前是茶与金平糖，切到不做主角会换号
        self.assertEqual(prev["changes"]["tieba"], ["BDUSS"])
        self.assertEqual(prev["changes"]["bilibili"], ["SESSDATA"])
        # 预览不写盘
        self.assertEqual(self._bduss(), MINE)
        # 预设里没有的平台：会被整块移除（提醒用户）
        self.assertIn("drops", prev)

    def test_save_without_apply_keeps_current_account(self):
        """只存预设不应用：不动 config.yaml，也不改「当前」。"""
        self._save_profile("不做主角", HAKRE, "bili-hakre")
        other = self._save_profile("备用号", MINE, "bili-mine", apply=False)
        self.assertFalse(other["applied"])
        self.assertEqual(self._bduss(), HAKRE)
        active = self._get("/api/config-profiles")["active"]
        self.assertEqual(active, "不做主角")

    def test_profiles_are_listed_with_active_flag(self):
        self._save_profile("不做主角", HAKRE, "bili-hakre")
        self._save_profile("茶与金平糖", MINE, "bili-mine")
        listing = self._get("/api/config-profiles")
        names = [p["name"] for p in listing["profiles"]]
        self.assertEqual(names, ["不做主角", "茶与金平糖"])
        self.assertEqual(listing["active"], "茶与金平糖")


if __name__ == "__main__":
    unittest.main()
