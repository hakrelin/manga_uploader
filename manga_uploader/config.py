"""配置加载：config.yaml + 内置默认值。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class ConfigError(RuntimeError):
    pass


@dataclass
class PlatformConfig:
    name: str
    enabled: bool = True
    cookies: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)


@dataclass
class CommonConfig:
    timeout: float = 30.0
    retries: int = 3
    interval_seconds: float = 0.0
    max_width: int = 2400
    max_height: int = 0
    quality: int = 88
    max_bytes_mb: float = 10.0
    output_dir: str = "output"
    dry_run: bool = False
    confirm: bool = True
    parallel: bool = False
    verbose: bool = False
    log_file: Optional[str] = None
    # 代理：proxy_url 手动指定 http(s)://…；use_system_proxy 读取 Windows 系统代理
    proxy_url: str = ""
    use_system_proxy: bool = False
    # 罗马音 AI 转换（OpenAI 兼容接口；空 api_key 时自动回退本地 pykakasi）
    ai_enabled: bool = False
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_prompt: str = ""
    ai_timeout: float = 60.0


@dataclass
class AppConfig:
    common: CommonConfig = field(default_factory=CommonConfig)
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    path: Optional[Path] = None


# 平台默认设置，配置文件只需覆盖差异（主要是 cookies）
DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "bilibili": {
        # article = 发布专栏文章；dynamic = 发布图文动态（旧行为，保留兼容）
        "publish_mode": "article",
        "tid": 4,                   # 4 = 单图封面模板（3 = 三图封面）
        "category": 0,              # 专栏分类 id，0 = 默认
        "original": 1,              # 1 = 声明原创，0 = 非原创
        "reprint": 0,               # 0 = 原创 / 1 = 转载（配合 original 一起提交）
        "max_article_pages": 100,   # 单篇专栏最多图片数，超出自动拆成多篇
        "upload_attempts": 3,       # 单张图片上传失败后的自动重试轮数
        "image_category": "draw",  # daily / draw / cos
        "max_pages_per_post": 9,   # 图文动态单条上限 9 张（仅 publish_mode=dynamic）
        "topics": ["#原创漫画#"],
        "use_system_proxy": False,  # 国内站默认直连，避免代理造成 SSL/风控问题
        "proxy_url": "",
    },
    "tieba": {
        "forum": "",
        "fid": 0,
        "max_pages_per_post": 9,   # 每楼最多 9 张（网页端硬上限）；第 1 楼固定只放封面
        "upload_sleep": 1.0,
        "title_suffix": "",
        "use_system_proxy": False,
        "proxy_url": "",
    },
    "ehentai": {
        "category_label": "Doujinshi",  # 默认同人志（汉化搬运）；可选 Non-H/Manga 等
        "rating_label": "",
        "language_label": "Chinese",  # 汉化上传默认中文；原版/无字可改 Japanese / No Text
        "extra_tags": [],
        "publish_after_upload": True,  # 上传文件后自动执行 Publish Gallery（False=只建草稿）
    },
    "zaimanhua": {
        "cate": "1",  # 1 原创作品 / 2 原创汉化 / 3 个人扫漫 / 4 转载作品
        "work_name": "",  # 作品(系列)名，留空用 manga.json 的 title
        "chapter_name": "",  # 章节名，留空用章节标题
        "max_pages_per_upload": 500,  # 单次提交页数上限
        "upload_attempts": 2,  # 单张图片上传失败自动重试次数（网络/服务器抽风）
        "use_system_proxy": False,  # 再漫画是国内站：默认直连，避免代理断连
        "proxy_url": "",  # 手动代理同样默认留空
    },
}

REQUIRED_COOKIES: dict[str, list[str]] = {
    "bilibili": ["SESSDATA", "bili_jct"],
    "tieba": ["BDUSS"],
    "ehentai": ["ipb_member_id", "ipb_pass_hash"],
    "zaimanhua": ["token"],
}


def _merge(default: dict, override: dict) -> dict:
    result = copy.deepcopy(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def find_config_file(path: Optional[str] = None) -> Path:
    if path:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            raise ConfigError(f"配置文件不存在：{candidate}")
        return candidate
    for name in ("config.yaml", "config.yml", "config.json"):
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "未找到 config.yaml，请先 cp config.example.yaml config.yaml 并填写账号 Cookie"
    )


def _read_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("需要 PyYAML：pip install pyyaml") from exc
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"配置文件顶层必须是键值对：{path}")
        return data
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败：{path}\n{exc}") from exc


def _read_json(path: Path) -> dict:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件解析失败：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是键值对：{path}")
    return data


def load_config(path: Optional[str] = None, *, dry_run: bool = False, confirm: bool | None = None) -> AppConfig:
    config_path = find_config_file(path)
    raw = _read_yaml(config_path) if config_path.suffix.lower() != ".json" else _read_json(config_path)

    common_raw = raw.get("common", {}) if isinstance(raw.get("common"), dict) else {}
    allowed_fields = set(CommonConfig().__dataclass_fields__)
    common_values = {
        key: value for key, value in common_raw.items() if key in allowed_fields
    }
    common = CommonConfig(**{**CommonConfig().__dict__, **common_values})
    if dry_run:
        common.dry_run = True
    if confirm is not None:
        common.confirm = confirm

    platforms_raw = raw.get("platforms", {})
    platforms: dict[str, PlatformConfig] = {}
    for name, defaults in DEFAULT_SETTINGS.items():
        item = platforms_raw.get(name, {}) if isinstance(platforms_raw, dict) else {}
        if item is None:
            item = {}
        enabled = item.get("enabled", True) if isinstance(item.get("enabled"), bool) else True
        cookies = item.get("cookies", {}) if isinstance(item.get("cookies"), dict) else {}
        settings_raw = item.get("settings", {}) if isinstance(item.get("settings"), dict) else {}
        if isinstance(item, dict):
            # 兼容扁平写法：设置直接写在平台下
            for key in list(item.keys()):
                if key not in ("enabled", "cookies", "settings"):
                    settings_raw.setdefault(key, item[key])
        settings = _merge(defaults, settings_raw)
        platforms[name] = PlatformConfig(
            name=name,
            enabled=enabled,
            cookies={str(k): str(v) for k, v in cookies.items()},
            settings=settings,
        )

    return AppConfig(common=common, platforms=platforms, path=config_path)


def missing_cookies(cfg: PlatformConfig) -> list[str]:
    return [name for name in REQUIRED_COOKIES.get(cfg.name, []) if not cfg.cookies.get(name)]
