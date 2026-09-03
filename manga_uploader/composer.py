"""漫画元数据与各平台标题/正文自动组合。

所有发布器与 GUI 共用这一套规则，保证“GUI 预览 = 实际发布内容”。

manga.json 支持的通用键：
    title        中文标题（单行本/短篇直接用）
    author       画师/作者名（英文标题侧可用罗马音，JP 标题侧可直接填日文）
    circle       社团名
    event        发行展会，如 C105
    event_en     展会罗马音（自动生成，可手改）
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
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .comic import platform_meta
from .models import Chapter


# ---------- 日文 → 罗马音 ----------
#
# 两层引擎：
#   1) pykakasi（可选依赖）：内置词典，能把汉字读音、分词，输出与本站点约定一致的
#      ASCII/Hepburn 风格（とうきょう→toukyou、しゃしん→shashin、きって→kitte）。
#      未安装时回退到下面的本地假名表，汉字部分仍会保留原文。
#   2) 覆盖词典 data/romaji_overrides.json：pykakasi 会把同人专有名词（例大祭、
#      红魔郷、博麗神社等）逐字读错，此处按“最长原文匹配”先切块再统一转写。
#   3) AI 转换（可选，OpenAI 兼容接口）：本地引擎整体质量不足时可改用大模型
#      自动分词/读音，prompt 见 _ai_prompts；网络失败自动回退本地引擎。

_CJK_OR_KANA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uf900-\ufaff\u3400-\u4dbf]")
_KANA_ONLY = re.compile(r"[\u3040-\u30ff]")
# 需要独立成块的日文标点/分隔符（pykakasi 无法与两侧词合读；ー是长音记号不算）
_JP_SEPARATOR = re.compile(r"[\u30fb・—～]")


def _rom_loader() -> dict[str, str]:
    """加载内置覆盖词典（原文→假名读音）。"""
    path = Path(__file__).resolve().parent / "data" / "romaji_overrides.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if key.startswith("_") or not isinstance(key, str) or not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            out[key] = value
    return out


_OVERRIDES: dict[str, str] = _rom_loader()


def _has_jp(text: str) -> bool:
    return bool(_CJK_OR_KANA.search(text))


@lru_cache(maxsize=1)
def _pykakasi():
    """惰性导入 pykakasi；不可用时返回 None。"""
    try:
        import pykakasi  # type: ignore

        return pykakasi.kakasi()
    except Exception:
        return None


def romaji_engine_status() -> str:
    """给 GUI 用的引擎状态文案。"""
    return "pykakasi" if _pykakasi() is not None else "basic"


# ---------- AI 转换（OpenAI 兼容 Chat Completions） ----------

_AI_DEFAULT_NAME_PROMPT = (
    "你是日文罗马音转换器。把用户输入的日文（可能含汉字/假名/英文混合）转成 ASCII 罗马音，"
    "遵循 Hepburn 式习惯：し→shi、ち→chi、つ→tsu、じ→ji、ふ→fu、長音用双写元音"
    "（おう→ou、こう→kou、コーヒー→koohii），促音双写后续辅音（きって→kitte）。\n"
    "规则：\n"
    "1. 只输出转换结果，不解释、不加引号、不加 Markdown、不改写原文含义。\n"
    "2. 英文/数字原样保留，词间与日文间用半角空格分隔。\n"
    "3. 空格分隔的每个词首字母大写；专有名词按常识断词（社团/作者/展会名）。\n"
    "4. 不确定读音的汉字优先给出最常用音读/训读；无法确定的字保留原字，绝不编造。\n"
    "5. 输入本身已是拉丁字母时原样输出。\n"
    "示例：\n"
    "万能型天才肌美少女主人公の憂鬱 → Bannou-gata Tensai-hada Bishoujo Shujinkou no Yuuutsu\n"
    "一代大佐 → Ichidai Taisa\n"
    "サンシャインクリエイション → Sunshine Creation\n"
    "こんにちは 世界 → Konnichiwa Sekai"
)

_AI_DEFAULT_TITLE_PROMPT = (
    "你是日文标题罗马音转换器。把日文/日文混合标题转成适合 e-hentai 英文标题的罗马音，"
    "遵循 Hepburn 式习惯（し→shi、ち→chi、つ→tsu、じ→ji、ふ→fu；長音双写元音；"
    "促音双写；ん 在元音/や行前写作 n'）。\n"
    "规则：\n"
    "1. 只输出转换结果，不解释、不加引号、不加 Markdown。\n"
    "2. 助词（の/を/は/へ/が 等）独立成词并在首字母大写时小写（no、wo、ha 等）。\n"
    "3. 词语按语义断开，用空格分隔；同一熟语/词内若有明显结构（如 万能+型、天才+肌）"
    "可用连字符保持可读，连字符后也首字母大写；不要为每个汉字加连字符。\n"
    "4. 英文/数字原样保留。不确定读音的字保留原字，绝不编造。\n"
    "5. 输入已是罗马音时原样输出。\n"
    "示例：\n"
    "万能型天才肌美少女主人公の憂鬱 → Bannou-gata Tensai-hada Bishoujo Shujinkou no Yuuutsu\n"
    "俺の妹がこんなに可愛いわけがない → Ore no Imouto ga Konnani Kawaii Wake ga Nai\n"
    "東方紅楼夢 → Touhou Kouroumu"
)


def ai_config_is_ready(cfg: dict[str, Any]) -> bool:
    """是否具备 AI 转换条件（开关+地址+key+模型）。"""
    try:
        return bool(
            cfg.get("enabled")
            and str(cfg.get("base_url") or "").strip()
            and str(cfg.get("api_key") or "").strip()
            and str(cfg.get("model") or "").strip()
        )
    except Exception:
        return False


def _ai_endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _ai_request(text: str, system_prompt: str, cfg: dict[str, Any]) -> str:
    """同步调用 OpenAI 兼容接口；失败抛出带可读信息的异常。"""
    import requests  # 延迟导入，避免 GUI 首屏依赖

    base_url = str(cfg.get("base_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    timeout = float(cfg.get("timeout") or 60)
    url = _ai_endpoint(base_url)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    proxy_url = str(cfg.get("proxy_url") or "").strip() or None
    resp = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=timeout,
        proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"AI 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = (
        (data.get("choices") or [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    content = (content or "").strip()
    if not content:
        raise RuntimeError("AI 返回内容为空")
    return content


def ai_to_romaji(text: str, *, kind: str = "name", cfg: dict[str, Any] | None = None) -> str:
    """用 AI 把日文转成罗马音；kind=name/title 选择不同默认 prompt。

    cfg 结构：{enabled, base_url, api_key, model, prompt, timeout, proxy_url}。
    prompt 留空使用内置默认；失败（网络/接口/解析）抛异常由调用方决定是否回退。
    """
    if not text.strip():
        return ""
    cfg = cfg or {}
    prompt = str(cfg.get("prompt") or "").strip() or (
        _AI_DEFAULT_TITLE_PROMPT if kind == "title" else _AI_DEFAULT_NAME_PROMPT
    )
    raw = _ai_request(text, prompt, cfg)
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()
    raw = raw.strip("\"'“”‘’")
    return raw


def _normalize_width(text: str) -> str:
    """全角英数/空格归一为半角。"""
    return text.replace("　", " ").translate(
        str.maketrans(
            "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        )
    )


def _capitalize_word(word: str) -> str:
    """首字母大写（已大写/数字开头则原样）。"""
    if word and word[:1].isalpha() and word[:1].islower():
        return word[:1].upper() + word[1:]
    return word


def _convert_jp_chunk(chunk: str, pykk) -> str:
    """一个日文块的转换：优先 pykakasi；命中词典的整词用覆盖读音；否则回退本地表。"""
    if pykk is not None:
        try:
            tokens = pykk.convert(chunk)
            converted = [tok["hepburn"].strip() or tok["orig"] for tok in tokens]
            roma = " ".join(p for p in converted if p)
            if roma:
                return roma
        except Exception:
            pass
    # 回退：词典整词 / 逐字词典 / 本地表（汉字仍原样保留，不丢字）
    if chunk in _OVERRIDES:
        return _to_romaji_local(_OVERRIDES[chunk])
    if _KANA_ONLY.search(chunk):
        return _to_romaji_local(chunk)
    if _OVERRIDES:
        parts: list[str] = []
        for char in chunk:
            if char in _OVERRIDES:
                parts.append(_to_romaji_local(_OVERRIDES[char]))
            else:
                parts.append(char)
        return "".join(parts)
    return _to_romaji_local(chunk)


def _to_romaji_impl(text: str, title_case: bool) -> str:
    """按“日文块 / 非日文块”分段，分别转换后合并。"""
    if not text:
        return ""
    normalized = _normalize_width(text)
    pykk = _pykakasi()
    keys = sorted((k for k in _OVERRIDES if k in normalized), key=len, reverse=True)
    parts: list[str] = []
    i = 0
    n = len(normalized)
    while i < n:
        hit = ""
        for key in keys:
            if normalized.startswith(key, i):
                hit = key
                break
        if hit:
            reading = _OVERRIDES[hit]
            if _has_jp(reading):
                if _CJK_OR_KANA.search(reading) and not _KANA_ONLY.fullmatch(reading):
                    # reading 含汉字：递归转换
                    value = _to_romaji_impl(reading, title_case)
                else:
                    # reading 为纯假名：用本地表整词转写，避免被再分词
                    value = _to_romaji_local(reading)
                    if title_case:
                        value = _capitalize_word(value)
            else:
                value = reading
            parts.append(value)
            i += len(hit)
            continue
        ch = normalized[i]
        is_jp = _CJK_OR_KANA.match(ch) is not None
        j = i + 1
        while j < n:
            nxt = normalized[j]
            if _JP_SEPARATOR.fullmatch(ch) or _JP_SEPARATOR.match(nxt):
                break
            if _CJK_OR_KANA.match(nxt) is not None:
                if not is_jp:
                    break
            elif is_jp:
                break
            # 若后续某词典键从这里开始，也先停在此处让外层处理
            if any(normalized.startswith(k, j) for k in keys):
                break
            j += 1
        block = normalized[i:j]
        if block.isspace() or _JP_SEPARATOR.fullmatch(block):
            parts.append(" ")
        elif is_jp:
            parts.append(_convert_jp_chunk(block, pykk))
        else:
            parts.append(block)
        i = j
    joined = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if title_case:
        out: list[str] = []
        for part in re.split(r"(\s+|-+)", joined):
            if part and part[:1].isalpha() and part[:1].islower():
                out.append(part[:1].upper() + part[1:])
            else:
                out.append(part)
        return "".join(out)
    return joined


def to_romaji(text: str) -> str:
    """日文 → 罗马音（ASCII 风格，小写；如 とうきょう→toukyou、例大祭→reitaisai）。

    - pykakasi 可用：汉字读音、分词（万能型→bannougata）
    - 未安装：仅本地假名表；词典外的汉字保留原样
    """
    return _to_romaji_impl(text, title_case=False)


def to_romaji_title_case(text: str) -> str:
    """转罗马音并把每个词/连字符段首字母大写（例：たいさんち→Taisanchi、例大祭→Reitaisai）。"""
    return _to_romaji_impl(text, title_case=True)


# ---------- 本地假名 → 罗马音（无 pykakasi 时的回退；表 とうきょう→toukyou） ----------

_BASE: dict[str, str] = {
    # 平假名
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "wo", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ゐ": "wi", "ゑ": "we",
    # 片假名
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "wo", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "ヴ": "vu", "ヷ": "va", "ヸ": "vi", "ヹ": "ve", "ヺ": "vo",
    "ヰ": "wi", "ヱ": "we",
}

_SMALL: dict[str, str] = {
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
    "ャ": "ya", "ュ": "yu", "ョ": "yo", "ヮ": "wa",
}

# 基本假名 + 小写假名的拗音
_DIGRAPH: dict[tuple[str, str], str] = {
    ("き", "ゃ"): "kya", ("き", "ゅ"): "kyu", ("き", "ょ"): "kyo",
    ("ぎ", "ゃ"): "gya", ("ぎ", "ゅ"): "gyu", ("ぎ", "ょ"): "gyo",
    ("し", "ゃ"): "sha", ("し", "ゅ"): "shu", ("し", "ょ"): "sho",
    ("じ", "ゃ"): "ja", ("じ", "ゅ"): "ju", ("じ", "ょ"): "jo",
    ("ち", "ゃ"): "cha", ("ち", "ゅ"): "chu", ("ち", "ょ"): "cho",
    ("ぢ", "ゃ"): "ja", ("ぢ", "ゅ"): "ju", ("ぢ", "ょ"): "jo",
    ("に", "ゃ"): "nya", ("に", "ゅ"): "nyu", ("に", "ょ"): "nyo",
    ("ひ", "ゃ"): "hya", ("ひ", "ゅ"): "hyu", ("ひ", "ょ"): "hyo",
    ("び", "ゃ"): "bya", ("び", "ゅ"): "byu", ("び", "ょ"): "byo",
    ("ぴ", "ゃ"): "pya", ("ぴ", "ゅ"): "pyu", ("ぴ", "ょ"): "pyo",
    ("み", "ゃ"): "mya", ("み", "ゅ"): "myu", ("み", "ょ"): "myo",
    ("り", "ゃ"): "rya", ("り", "ゅ"): "ryu", ("り", "ょ"): "ryo",
    ("キ", "ャ"): "kya", ("キ", "ュ"): "kyu", ("キ", "ョ"): "kyo",
    ("ギ", "ャ"): "gya", ("ギ", "ュ"): "gyu", ("ギ", "ョ"): "gyo",
    ("シ", "ャ"): "sha", ("シ", "ュ"): "shu", ("シ", "ョ"): "sho",
    ("ジ", "ャ"): "ja", ("ジ", "ュ"): "ju", ("ジ", "ョ"): "jo",
    ("チ", "ャ"): "cha", ("チ", "ュ"): "chu", ("チ", "ョ"): "cho",
    ("ニ", "ャ"): "nya", ("ニ", "ュ"): "nyu", ("ニ", "ョ"): "nyo",
    ("ヒ", "ャ"): "hya", ("ヒ", "ュ"): "hyu", ("ヒ", "ョ"): "hyo",
    ("ビ", "ャ"): "bya", ("ビ", "ュ"): "byu", ("ビ", "ョ"): "byo",
    ("ピ", "ャ"): "pya", ("ピ", "ュ"): "pyu", ("ピ", "ョ"): "pyo",
    ("ミ", "ャ"): "mya", ("ミ", "ュ"): "myu", ("ミ", "ョ"): "myo",
    ("リ", "ャ"): "rya", ("リ", "ュ"): "ryu", ("リ", "ョ"): "ryo",
}

# 片假名外来语常见双假名音节（ファ=ファ …）
_PAIR_SPECIAL: dict[str, str] = {
    "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "フュ": "fyu",
    "ウィ": "wi", "ウェ": "we", "ウォ": "wo",
    "ヴァ": "va", "ヴィ": "vi", "ヴェ": "ve", "ヴォ": "vo", "ヴュ": "vyu",
    "チェ": "che", "シェ": "she", "ジェ": "je", "ティ": "ti", "ディ": "di",
    "トゥ": "tu", "ドゥ": "du", "テュ": "tyu", "デュ": "dyu",
    "クァ": "kwa", "クィ": "kwi", "クェ": "kwe", "クォ": "kwo",
    "グァ": "gwa", "グィ": "gwi", "グェ": "gwe", "グォ": "gwo",
    "ツァ": "tsa", "ツィ": "tsi", "ツェ": "tse", "ツォ": "tso",
    "スィ": "si", "ズィ": "zi",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo", "ふゅ": "fyu",
    "うぃ": "wi", "うぇ": "we", "うぉ": "wo",
    "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo", "ゔゅ": "vyu",
}

def _geminate(next_roma: str) -> str:
    """促音っ 加在下个音节前。"""
    if not next_roma:
        return ""
    if next_roma.startswith("ch"):
        # っち→tchi / っちゃ→tcha（Hepburn 用 t 接 ch）
        return "t" + next_roma
    first = next_roma[0]
    if first.isalpha():
        return first + next_roma
    return ""


def _to_romaji_local(text: str) -> str:
    """本地假名表回退实现：只处理假名音节，汉字/无法识别字保留原样。"""
    if not text:
        return ""
    # 全角英数/空格归一
    normalized = (
        text.replace("　", " ")
        .translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    )
    out: list[str] = []
    i = 0
    pending_geminate = False
    length = len(normalized)
    while i < length:
        ch = normalized[i]
        pair = normalized[i : i + 2]
        if pair in _PAIR_SPECIAL:
            roma = _PAIR_SPECIAL[pair]
            if pending_geminate:
                out.append(_geminate(roma))
                pending_geminate = False
            else:
                out.append(roma)
            i += 2
            continue
        next_ch = normalized[i + 1] if i + 1 < length else ""
        if ch in _BASE and next_ch in _SMALL and (ch, next_ch) in _DIGRAPH:
            roma = _DIGRAPH[(ch, next_ch)]
            if pending_geminate:
                out.append(_geminate(roma))
                pending_geminate = False
            else:
                out.append(roma)
            i += 2
            continue
        if ch in ("っ", "ッ"):
            pending_geminate = True
            i += 1
            continue
        if ch == "ー":
            # 长音符：重复上一个音节末尾的元音（ASCII 习惯，如 コーヒー→koohii）
            if out:
                last = out[-1]
                if last and last[-1] in "aeiou":
                    out.append(last[-1])
            i += 1
            continue
        roma = _BASE.get(ch)
        if roma is None:
            # 汉字、符号、小写假名等原样保留，避免“漏字”
            if pending_geminate and ch not in (" ", "-", "・"):
                out.append("tsu")
                pending_geminate = False
            out.append(ch)
            i += 1
            continue
        if roma == "n" and next_ch in _BASE and _BASE[next_ch][:1] in ("a", "i", "u", "e", "o", "y"):
            roma = "n'"
        if pending_geminate:
            out.append(_geminate(roma))
            pending_geminate = False
        else:
            out.append(roma)
        i += 1
    if pending_geminate:
        out.append("tsu")
    result = "".join(out)
    result = re.sub(r"\s+", " ", result).strip()
    return result


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
        "event_en": meta_field(chapter, platform, ("event_en",)),
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
    if _str(romaji):
        return _str(romaji)
    roma = to_romaji_title_case(value)
    return roma if roma != value else value


def _romaji_or_raw(value: str) -> str:
    roma = to_romaji(value)
    return roma if roma and roma != value else value


def _romaji_title_or_raw(value: str) -> str:
    roma = to_romaji_title_case(value)
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
    event = _str(f["event_en"]) or _romaji_title_or_raw(_str(f["event"]))
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


# ---------- 小黑盒 ----------

def xiaoheihe_title(chapter: Chapter) -> str:
    """小黑盒标题：中文标题（平台覆盖优先），服务端上限 30 字。"""
    meta = platform_meta(chapter, "xiaoheihe")
    if str(meta.get("title") or "").strip():
        return str(meta.get("title") or "").strip()
    f = fields(chapter, "xiaoheihe")
    title = str(f["title"]) or chapter.title
    return title[:30]


def xiaoheihe_body(chapter: Chapter) -> str:
    """小黑盒正文：作者/社团/简介（平台有整段覆盖时原样使用）。"""
    meta = platform_meta(chapter, "xiaoheihe")
    if str(meta.get("description") or "").strip():
        return str(meta.get("description") or "").strip()
    f = fields(chapter, "xiaoheihe")
    return build_credit_lines(f["author"], f["circle"], f["description"])


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
    "xiaoheihe": [
        {"key": "title", "label": "标题（≤30 字）", "kind": "text"},
        {"key": "description", "label": "正文（作者/社团/简介）", "kind": "textarea"},
    ],
}
