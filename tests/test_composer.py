import unittest
from pathlib import Path

from manga_uploader import composer
from manga_uploader.models import Chapter


def _chapter(**extra) -> Chapter:
    raw = {
        "event": "C105",
        "author": "たいさんち",
        "author_en": "Taisanchi",
        "circle": "一代大佐",
        "circle_en": "Ichidai Taisa",
        "title": "万能型天才肌美少女主人公的忧郁",
        "title_jp": "万能型天才肌美少女主人公の憂鬱",
        "title_en": "Bannou-gata Tensai-hada Bishoujo Shujinkou no Yuuutsu",
        "series": "东方",
        "series_en": "Touhou Project",
        "series_jp": "東方Project",
        "group": "茶与金平糖汉化组",
        "description": "测试简介",
    }
    raw.update(extra)
    return Chapter(
        key="root",
        title=str(raw["title"]),
        description=str(raw.get("description", "")),
        author=str(raw.get("author", "")),
        tags=list(raw.get("tags") or []),
        pages=[Path("001.png")],
        raw=raw,
    )


class TestComposer(unittest.TestCase):
    def test_to_romaji(self):
        self.assertEqual(composer.to_romaji("たいさんち"), "taisanchi")
        self.assertEqual(composer.to_romaji("こんにちは"), "konnichiha")
        self.assertEqual(composer.to_romaji("とうきょう"), "toukyou")
        self.assertEqual(composer.to_romaji("しゃしん"), "shashin")
        self.assertEqual(composer.to_romaji("きって"), "kitte")
        self.assertEqual(composer.to_romaji("いっち"), "itchi")
        self.assertEqual(composer.to_romaji("コーヒー"), "koohii")
        self.assertEqual(composer.to_romaji("コミックマーケット"), "komikkumaaketto")
        self.assertEqual(composer.to_romaji_title_case("たいさんち"), "Taisanchi")
        self.assertEqual(
            composer.to_romaji_title_case("いちだい たいさ"), "Ichidai Taisa"
        )
    def test_kanji_readings(self):
        # pykakasi 可用时自动读汉字；不可用时保持原样不丢字
        value = composer.to_romaji("例大祭")
        if composer.romaji_engine_status() == "pykakasi":
            self.assertEqual(value, "reitaisai")
            self.assertEqual(composer.to_romaji_title_case("博麗神社例大祭"), "Hakurei Jinja Reitaisai")
            self.assertEqual(composer.to_romaji_title_case("鈴仙・優曇華院・イナバ"), "Reisen Udongein Inaba")
        else:
            # 覆盖词典在无 pykakasi 时仍生效（读音存为假名）
            self.assertEqual(value, "reitaisai")

    def test_romaji_mixed_and_ascii(self):
        if composer.romaji_engine_status() != "pykakasi":
            self.skipTest("需要 pykakasi")
        # ASCII 原样保留不被小写化
        self.assertEqual(composer.to_romaji_title_case("東方Project"), "Touhou Project")
        # 汉字标题自动读出读音，词间按语义分词
        self.assertEqual(
            composer.to_romaji_title_case("万能型天才肌美少女主人公の憂鬱"),
            "Bannougata Tensai Hada Bishoujo Shujinkou No Yuuutsu",
        )
        self.assertEqual(composer.to_romaji("こんにちは 世界"), "konnichiha sekai")

    def test_ehentai_title_en_matches_example_shape(self):
        title = composer.ehentai_title_en(_chapter())
        self.assertEqual(
            title,
            "(C105) [Taisanchi (Ichidai Taisa)] Bannou-gata Tensai-hada "
            "Bishoujo Shujinkou no Yuuutsu | 万能型天才肌美少女主人公的忧郁 "
            "(Touhou Project) [Chinese] [茶与金平糖汉化组]",
        )

    def test_ehentai_title_jp_matches_example_shape(self):
        title = composer.ehentai_title_jp(_chapter())
        self.assertEqual(
            title,
            "(C105) [たいさんち (一代大佐)] 万能型天才肌美少女主人公の憂鬱 "
            "(東方Project) [中国翻訳] [茶与金平糖汉化组]",
        )

    def test_platform_title_and_body(self):
        chapter = _chapter()
        for platform in ("bilibili", "tieba"):
            self.assertEqual(
                composer.platform_title(chapter, platform),
                "【茶与金平糖汉化组】万能型天才肌美少女主人公的忧郁",
            )
            body = composer.platform_body(chapter, platform)
            self.assertIn("作者：たいさんち", body)
            self.assertIn("社团：一代大佐", body)
            self.assertIn("简介：测试简介", body)

    def test_ehentai_comment_and_zaim(self):
        chapter = _chapter()
        comment = composer.ehentai_comment(chapter)
        self.assertIn("作者：たいさんち", comment)
        self.assertIn("社团：一代大佐", comment)
        self.assertIn("简介：测试简介", comment)

        zaim = _chapter(tags=["东方", "汉化"])
        intro = composer.zaim_introduction(zaim)
        self.assertTrue(intro.startswith("东方\n"))
        self.assertIn("作者：たいさんち", intro)
        self.assertEqual(composer.zaim_work_name(zaim), "万能型天才肌美少女主人公的忧郁")
        self.assertEqual(composer.zaim_chapter_name(zaim), "短篇")

    def test_platform_override_wins(self):
        chapter = _chapter()
        chapter.raw.setdefault("platforms", {})["ehentai"] = {
            "gname_en": "手改的英文标题"
        }
        self.assertEqual(composer.ehentai_title_en(chapter), "手改的英文标题")

    def test_event_romaji_used_in_en_title_only(self):
        chapter = _chapter(event="サンシャインクリエイション")
        en = composer.ehentai_title_en(chapter)
        jp = composer.ehentai_title_jp(chapter)
        self.assertIn("(Sanshainkurieishon)", en)
        self.assertIn("(サンシャインクリエイション)", jp)


if __name__ == "__main__":
    unittest.main()
