"""漫画元数据与各平台标题/正文自动组合。

所有发布器与 GUI 共用这一套规则，保证“GUI 预览 = 实际发布内容”。

manga.json 支持的通用键：
    title        中文标题（单行本/短篇直接用）
    author       画师/作者名（英文标题侧可用罗马音，JP 标题侧可直接填日文）
    circle       社团名
    event        发行展会，如 C105
    group        汉化组名，如 茶与金平糖汉化组
    title_jp     日文原标题
    title_en     英文/罗马音标题（可用“日→罗马音”辅助生成）
    series       系列中文名/tag，如 东方
    series_en    系列英文名，如 Touhou Project
    series_jp    系列日文名，如 東方Project
    language     语言（默认 Chinese / 中文）
    chapter_name 再漫画章节名（默认“短篇”）
    tags         标签列表
    description  简介

每个平台可在 manga.json 的 platforms.<key> 下放覆盖值：
    ehentai  : gname_en / gname_jp / comment / category / langtype / language
    bilibili : title / description（整段正文）
    tieba    : title / description（整段正文）/ forum
    zaimanhua: work_name / chapter_name / introduction / cate
有覆盖值时以覆盖值为准；没有则按本模块规则自动组合。
"""

from __future__ import annotations

import re
from typing import Any

from .comic import platform_meta
from .models import Chapter


# ---------- 日文假名 → 罗马音（平文式简化版） ----------

_KANA_ROWS: list[tuple[str, str, str]] = [
    # 行 あ
    ("あ", "ぁ", "a"), ("い", "ぃ", "i"), ("う", "ぅ", "u"),
    ("え", "ぇ", "e"), ("お", "ぉ", "o"),
    # 行 か
    ("か", "が", "ka"), ("き", "ぎ", "ki"), ("く", "ぐ", "ku"),
    ("け", "げ", "ke"), ("こ", "ご", "ko"),
    # 行 さ
    ("さ", "ざ", "sa"), ("し", "じ", "shi"), ("す", "ず", "su"),
    ("せ", "ぜ", "se"), ("そ", "ぞ", "so"),
    # 行 た
    ("た", "だ", "ta"), ("ち", "ぢ", "chi"), ("つ", "づ", "tsu"),
    ("て", "で", "te"), ("と", "ど", "to"),
    # 行 な
    ("な", "", "na"), ("に", "", "ni"), ("ぬ", "", "nu"),
    ("ね", "", "ne"), ("の", "", "no"),
    # 行 は
    ("は", "ば", "ha"), ("ひ", "び", "hi"), ("ふ", "ぶ", "fu"),
    ("へ", "べ", "he"), ("ほ", "ぼ", "ho"),
    # 行 ま
    ("ま", "", "ma"), ("み", "", "mi"), ("む", "", "mu"),
    ("め", "", "me"), ("も", "", "mo"),
    # 行 や
    ("や", "ゃ", "ya"), ("ゆ", "ゅ", "yu"), ("よ", "ょ", "yo"),
    # 行 ら
    ("ら", "", "ra"), ("り", "", "ri"), ("る", "", "ru"),
    ("れ", "", "re"), ("ろ", "", "ro"),
    # 行 わ
    ("わ", "", "wa"), ("を", "", "wo"), ("ん", "", "n"),
]


def _build_kana_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for hira, daku, roma in _KANA_ROWS:
        mapping[hira] = roma
        mapping[hira.upper()] = roma  # 片假名直接由大写 ASCII 近似，下面再补真实表
    extra = {
        "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
        "カ": "ka", "ガ": "ga", "キ": "ki", "ギ": "gi", "ク": "ku", "グ": "gu",
        "ケ": "ke", "ゲ": "ge", "コ": "ko", "ゴ": "go",
        "サ": "sa", "ザ": "za", "シ": "shi", "ジ": "ji", "ス": "su", "ズ": "zu",
        "セ": "se", "ゼ": "ze", "ソ": "so", "ゾ": "zo",
        "タ": "ta", "ダ": "da", "チ": "chi", "ヂ": "ji", "ツ": "tsu", "ヅ": "zu",
        "テ": "te", "デ": "de", "ト": "to", "ド": "do",
        "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
        "ハ": "ha", "バ": "ba", "パ": "pa", "ヒ": "hi", "ビ": "bi", "ピ": "pi",
        "フ": "fu", "ブ": "bu", "プ": "pu", "ヘ": "he", "ベ": "be", "ペ": "pe",
        "ホ": "ho", "ボ": "bo", "ポ": "po",
        "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
        "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
        "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
        "ワ": "wa", "ヲ": "wo", "ン": "n",
        "ヴ": "vu",
        "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
        "ャ": "ya", "ュ": "yu", "ョ": "yo",
        "ー": "", "ッ": "", "ッ": "",
    }
    mapping.update(extra)
    return mapping


_KANA = _build_kana_map()


def to_romaji(text: str) -> str:
    """把日文假名转成平文式罗马音；汉字无法自动判断读音时会原样保留，
    请结合站点习惯手动改成罗马音（如 万能型→Bannou-gata）。"""
    if not text:
        return ""
    result: list[str] = []
    for ch in text:
        roma = _KANA.get(ch)
        if roma is None:
            result.append(ch)
            continue
        if ch in ("っ", "ッ"):
            # 促音：若后面还有字符，先占位由下个字符首字母补齐
            result.append("__TSU__")
            continue
        result.append(roma)
    # 处理促音（っ/ッ）：下个音节首字母双写
    out = "".join(result)
    while "__TSU__" in out:
        out = re.sub(
            r"__TSU__([a-zA-Z])",
            lambda m: m.group(1) + m.group(1),
            out,
            count=1,
        )
        out = out.replace("__TSU__", "", 1) if "__TSU__" in out else out
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ---------- 读取元数据 ----------

def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,，、\n]", value) if part.strip()]
    return [_str(item) for item in value if _str(item)]


def meta_field(chapter: Chapter, platform: str | None, keys: tuple[str, ...]) -> str:
    """取值顺序：platforms.<platform> → 根/章节合并 raw → 章节内置字段。"""
    if platform:
        meta = platform_meta(chapter, platform)
        for key in keys:
            if _str(meta.get(key)):
                return _str(meta.get(key))
    for key in keys:
        if _str(chapter.raw.get(key)):
            return _str(chapter.raw.get(key))
    fallback = {
        "title": chapter.title,
        "author": chapter.author,
        "description": chapter.description,
    }
    for key in keys:
        if _str(fallback.get(key)):
            return _str(fallback[key])
    return ""


def fields(chapter: Chapter, platform: str | None = None) -> dict[str, Any]:
    """汇总单个平台用到的全部基础字段。"""
    tags = _list(platform_meta(chapter, platform).get("tags") if platform else None)
    if not tags:
        tags = _list(chapter.raw.get("tags")) or list(chapter.tags)
    return {
        "title": meta_field(chapter, platform, ("title",)),
        "author": meta_field(chapter, platform, ("author",)),
        "author_en": meta_field(chapter, platform, ("author_en",)),
        "circle": meta_field(chapter, platform, ("circle", "社团")),
        "circle_en": meta_field(chapter, platform, ("circle_en",)),
        "event": meta_field(chapter, platform, ("event", "展会")),
        "group": meta_field(chapter, platform, ("group", "group_name", "汉化组")),
        "title_jp": meta_field(chapter, platform, ("title_jp", "title_original", "title_jpn")),
        "title_en": meta_field(chapter, platform, ("title_en",)),
        "series": meta_field(chapter, platform, ("series", "series_cn")),
        "series_en": meta_field(chapter, platform, ("series_en",)),
        "series_jp": meta_field(chapter, platform, ("series_jp",)),
        "language": meta_field(chapter, platform, ("language",)) or "Chinese",
        "chapter_name": meta_field(chapter, platform, ("chapter_name",)),
        "description": meta_field(chapter, platform, ("description",)),
        "tags": tags,
    }


def _fill_romaji_field(value: str, romaji: str) -> str:
    return _str(romaji) or to_romaji(value)


def _romaji_or_raw(value: str) -> str:
    roma = to_romaji(value)
    return roma if roma and roma != value else value


# ---------- e-hentai ----------

_EH_CATEGORIES = [
    "Doujinshi",
    "Manga",
    "Artist CG",
    "Game CG",
    "Western",
    "Non-H",
    "Image Set",
    "Cosplay",
    "Asian Porn",
    "Misc",
]


def ehentai_categories() -> list[str]:
    return list(_EH_CATEGORIES)


def ehentai_title_en(chapter: Chapter) -> str:
    f = fields(chapter, "ehentai")
    override = platform_meta(chapter, "ehentai").get("gname_en")
    if _str(override):
        return _str(override)
    event = _str(f["event"])
    author = _fill_romaji_field(f["author"], f["author_en"])
    circle = _fill_romaji_field(f["circle"], f["circle_en"])
    title_en = _str(f["title_en"]) or _romaji_or_raw(f["title_jp"])
    title_cn = _str(f["title"])
    series = _str(f["series_en"]) or _romaji_or_raw(_str(f["series_jp"])) or _str(f["series"])
    main = title_en if title_en else title_cn
    if title_en and title_cn and title_cn != title_en:
        main = f"{title_en} | {title_cn}"
    parts: list[str] = []
    if event:
        parts.append(f"({event})")
    bracket = ""
    if author:
        bracket = _str(author)
    if circle:
        bracket = f"{bracket} ({circle})" if bracket else _str(circle)
    if bracket:
        parts.append(f"[{bracket}]")
    if main:
        parts.append(main)
    if series:
        parts[-1] = f"{parts[-1]} ({series})"
    language = _str(f["language"]).lower()
    if "chinese" in language or "中文" in language or "中国" in language or f["group"]:
        parts.append("[Chinese]")
    if f["group"]:
        parts.append(f"[{f['group']}]")
    return " ".join(parts)


def ehentai_title_jp(chapter: Chapter) -> str:
    f = fields(chapter, "ehentai")
    override = platform_meta(chapter, "ehentai").get("gname_jp")
    if _str(override):
        return _str(override)
    event = _str(f["event"])
    author = _str(f["author"])
    circle = _str(f["circle"])
    title_jp = _str(f["title_jp"])
    series_jp = _str(f["series_jp"]) or _str(f["series_en"]) or _str(f["series"])
    parts: list[str] = []
    if event:
        parts.append(f"({event})")
    bracket = ""
    if author:
        bracket = author
    if circle:
        bracket = f"{bracket} ({circle})" if bracket else circle
    if bracket:
        parts.append(f"[{bracket}]")
    if title_jp:
        parts.append(title_jp)
    if series_jp:
        parts[-1] = f"{parts[-1]} ({series_jp})"
    if f["group"] or "chinese" in _str(f["language"]).lower() or "中文" in _str(f["language"]):
        parts.append("[中国翻訳]")
    if f["group"]:
        parts.append(f"[{f['group']}]")
    return " ".join(parts)


def ehentai_comment(chapter: Chapter) -> str:
    meta = platform_meta(chapter, "ehentai")
    if _str(meta.get("comment")):
        return _str(meta.get("comment"))
    f = fields(chapter, "ehentai")
    return build_credit_lines(f["author"], f["circle"], f["description"])


# ---------- 通用署名行 ----------

def build_credit_lines(author: str, circle: str, description: str) -> str:
    """作者/社团/简介 三段式组合（B站、贴吧正文与 E 站上传者评论共用）。"""
    lines: list[str] = []
    if _str(author):
        lines.append(f"作者：{_str(author)}")
    if _str(circle):
        lines.append(f"社团：{_str(circle)}")
    if _str(description):
        lines.append(f"简介：{_str(description)}")
    return "\n".join(lines)


def platform_title(chapter: Chapter, platform: str) -> str:
    """B站/贴吧共用标题：【汉化组】中文标题。"""
    meta = platform_meta(chapter, platform)
    if _str(meta.get("title")):
        return _str(meta.get("title"))
    f = fields(chapter, platform)
    title = _str(f["title"])
    group = _str(f["group"])
    if group:
        return f"【{group}】{title}"
    return title


def platform_body(chapter: Chapter, platform: str) -> str:
    """B站/贴吧共用正文：作者/社团/简介。平台有整段覆盖时原样使用。"""
    meta = platform_meta(chapter, platform)
    if _str(meta.get("description")):
        return _str(meta.get("description"))
    f = fields(chapter, platform)
    return build_credit_lines(f["author"], f["circle"], f["description"])


# ---------- 再漫画 ----------

def zaim_work_name(chapter: Chapter) -> str:
    meta = platform_meta(chapter, "zaimanhua")
    if _str(meta.get("work_name")):
        return _str(meta.get("work_name"))
    return _str(fields(chapter, "zaimanhua")["title"]) or chapter.title


def zaim_chapter_name(chapter: Chapter) -> str:
    meta = platform_meta(chapter, "zaimanhua")
    if _str(meta.get("chapter_name")):
        return _str(meta.get("chapter_name"))
    f = fields(chapter, "zaimanhua")
    return _str(f["chapter_name"]) or "短篇"


def zaim_introduction(chapter: Chapter) -> str:
    meta = platform_meta(chapter, "zaimanhua")
    if _str(meta.get("introduction")):
        return _str(meta.get("introduction"))
    f = fields(chapter, "zaimanhua")
    lines: list[str] = []
    first_tag = _str(f["series"]) or (_list(f["tags"])[0] if _list(f["tags"]) else "")
    if first_tag:
        lines.append(first_tag)
    if f["author"]:
        lines.append(f"作者：{f['author']}")
    if f["description"]:
        lines.append(f"简介：{f['description']}")
    return "\n".join(lines)


# 供 GUI 展示的结构化字段（每个平台按上传表单陈列）
PLATFORM_SCHEMA: dict[str, list[dict[str, str]]] = {
    "ehentai": [
        {"key": "category", "label": "画廊类型", "kind": "select"},
        {"key": "language", "label": "画廊语言（默认 Chinese）", "kind": "text"},
        {"key": "langtype", "label": "语言类型", "kind": "select"},
        {"key": "gname_en", "label": "英文标题", "kind": "text"},
        {"key": "gname_jp", "label": "日文标题", "kind": "text"},
        {"key": "comment", "label": "上传者评论", "kind": "textarea"},
    ],
    "bilibili": [
        {"key": "title", "label": "标题（【汉化组】中文标题）", "kind": "text"},
        {"key": "description", "label": "正文（作者/社团/简介）", "kind": "textarea"},
    ],
    "tieba": [
        {"key": "forum", "label": "目标吧名", "kind": "text"},
        {"key": "title", "label": "标题（【汉化组】中文标题）", "kind": "text"},
        {"key": "description", "label": "正文（作者/社团/简介）", "kind": "textarea"},
    ],
    "zaimanhua": [
        {"key": "work_name", "label": "标题（中文标题）", "kind": "text"},
        {"key": "chapter_name", "label": "章节名（默认短篇）", "kind": "text"},
        {"key": "introduction", "label": "简介（tag/作者/简介）", "kind": "textarea"},
        {"key": "cate", "label": "作品类型", "kind": "select"},
    ],
}
