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


class _Page:
    """_content_blocks 只需要 url/width/height 三个属性。"""

    def __init__(self, url: str, width: int, height: int) -> None:
        self.url = url
        self.width = width
        self.height = height


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

    # ---------- 文章正文里的图片（“发布成功但文章没图” 回归） ----------

    def test_article_blocks_embed_images_in_html(self):
        """文章渲染只认第一个 html 块：图片必须写进 html，否则文章里没有图。"""
        pages = [_Page("https://cdn.max-c.com/a.jpg", 800, 1200),
                 _Page("https://cdn.max-c.com/b.jpg", 0, 0)]
        blocks = X._content_blocks("作者：某人\n简介：测试", pages, article=True)
        self.assertEqual(blocks[0]["type"], "html")
        body = blocks[0]["text"]
        for page in pages:
            self.assertIn(f'<img src="{page.url}"', body)
            self.assertIn(f'data-original="{page.url}"', body)
        self.assertIn("作者：某人", body)
        # 图片顺序与正文在前
        self.assertLess(body.index("作者：某人"), body.index("a.jpg"))
        # 网页端行为：后面仍然带 img 块（图片列表/计数用）
        self.assertEqual([b["type"] for b in blocks[1:]], ["img", "img"])

    def test_image_text_blocks_stay_text_plus_img(self):
        """图文模式不受影响：text 块 + img 块（渲染端会自己拼成 <img>）。"""
        pages = [_Page("https://cdn.max-c.com/a.jpg", 10, 20)]
        blocks = X._content_blocks("正文", pages, article=False)
        self.assertEqual([b["type"] for b in blocks], ["text", "img"])
        self.assertNotIn("<img", blocks[0]["text"])

    def test_article_without_description_still_carries_images(self):
        pages = [_Page("https://cdn.max-c.com/a.jpg", 10, 20)]
        blocks = X._content_blocks("", pages, article=True)
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn("cdn.max-c.com/a.jpg", blocks[0]["text"])

    def test_img_html_escapes_url_and_keeps_size(self):
        page = _Page("https://cdn.max-c.com/a.jpg?x=1&y=2", 640, 960)
        tag = X._img_html(page)
        self.assertIn("x=1&amp;y=2", tag)
        self.assertIn('data-width="640"', tag)
        self.assertIn('data-height="960"', tag)


if __name__ == "__main__":
    unittest.main()
