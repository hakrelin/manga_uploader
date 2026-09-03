"""小黑盒修复回归测试：草稿默认、正文截断、cookie jar 清理、登录失效判定。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from manga_uploader.config import DEFAULT_SETTINGS, CommonConfig, PlatformConfig
from manga_uploader.models import Chapter
from manga_uploader.publishers import xiaoheihe as X


def _chapter(tmp: str) -> Chapter:
    return Chapter(
        key="root",
        title="测试",
        description="",
        tags=[],
        author="",
        source_dir=pathlib.Path(tmp),
        raw={"title": "测试"},
    )


class XiaoheiheFixTest(unittest.TestCase):
    def test_config_default_publish_draft_is_true(self):
        # README 声明的“默认只存草稿”应与配置默认一致
        self.assertTrue(DEFAULT_SETTINGS["xiaoheihe"]["publish_draft"])

    def test_plan_reflects_draft_default_without_merged_settings(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(name="xiaoheihe", cookies={"cookie": "pkey=A"}),
            CommonConfig(output_dir=tmp),
        )
        rows = "\n".join(pub.plan(_chapter(tmp)))
        self.assertIn("默认存草稿", rows)

    def test_plan_reflects_public_when_configured_false(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(
                name="xiaoheihe",
                cookies={"cookie": "pkey=A"},
                settings={"publish_draft": False},
            ),
            CommonConfig(output_dir=tmp),
        )
        rows = "\n".join(pub.plan(_chapter(tmp)))
        self.assertIn("发布后为公开内容", rows)

    def test_description_truncated_to_server_limit(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(name="xiaoheihe", cookies={"cookie": "pkey=A"}),
            CommonConfig(output_dir=tmp),
        )
        long_text = "很长的简介\n" * 9000  # > 30000 字
        chapter = _chapter(tmp)
        chapter.raw["platforms"] = {"xiaoheihe": {"description": long_text}}
        text = pub._description(chapter)
        self.assertLessEqual(len(text), X.MAX_DESC_CHARS)

    def test_cookie_jar_drops_bogus_cookie_entry_keeps_header(self):
        tmp = tempfile.mkdtemp()
        pub = X.XiaoheihePublisher(
            PlatformConfig(
                name="xiaoheihe",
                cookies={
                    "cookie": "pkey=A; heybox_id=123; x_xhh_tokenid=tk1",
                    "heybox_id": "123",
                },
            ),
            CommonConfig(output_dir=tmp),
        )
        names = [c.name for c in pub.http.session.cookies]
        self.assertNotIn("cookie", names)
        self.assertIn("heybox_id", names)  # 真 cookie 保留在 jar
        self.assertIn("pkey=A", pub.http.session.headers.get("Cookie", ""))
        self.assertEqual(pub.http.session.headers.get("x-xhh-token-id"), "tk1")

    def test_login_expired_classification(self):
        # 权限类提示（含“登录”字样）不应被判成登录失效
        expired = (
            "请登录后使用该功能",
            "登录已失效",
            "非法的请求",
            "登录已过期，请重新登录",
        )
        not_expired = ("该社区需要先登录后才能发言", "内部错误", "")
        for msg in expired:
            self.assertTrue(X.XiaoheihePublisher._is_expired(msg), msg)
        for msg in not_expired:
            self.assertFalse(X.XiaoheihePublisher._is_expired(msg), msg)


if __name__ == "__main__":
    unittest.main()
