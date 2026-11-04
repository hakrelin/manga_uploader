"""带 Cookie、重试、限速与调试转储的 requests 封装。"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .util import get_logger


class HttpError(RuntimeError):
    pass


def _clean_proxy_url(url: str) -> str:
    """把系统代理字符串整理成 http://host:port 形式。"""
    url = (url or "").strip()
    if not url:
        return ""
    # 形如 http=http://127.0.0.1:7890;https=... 的按协议配置
    match = re.search(r"https?=([^;]+)", url, re.I)
    if match:
        url = match.group(1).strip()
    if "://" not in url:
        url = "http://" + url
    return url


def detect_system_proxy() -> str:
    """读取系统代理（Windows 注册表 / 环境变量），返回 URL 或空串。"""
    for env_name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(env_name) or os.environ.get(env_name.lower())
        url = _clean_proxy_url(value or "")
        if url:
            return url
    if os.name == "nt":  # pragma: no cover - 仅 Windows
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)
            if enabled and server:
                return _clean_proxy_url(str(server))
        except OSError:
            pass
    return ""


# 出口 IP 查询（判断节点地区用）：各家返回的字段名不一样，逐个兜底
IP_LOOKUP_URLS = (
    "https://ipinfo.io/json",
    "https://api.ip.sb/geoip",
    "https://ipapi.co/json/",
)

# e-hentai 上传会被 Cloudflare 拦的地区（实测香港节点容易吃 403）
BLOCKED_UPLOAD_COUNTRIES = {"HK": "香港"}


def resolve_proxy(settings: Any, common: Any = None) -> tuple[str, str]:
    """决定**某个平台**走什么代理，返回 (代理地址, 一句说明)；地址为空 = 直连。

    规则只有三条，先到先得（没有“留空=跟随全局”那种猜谜）：
    1. `proxy_url` 填了 → 就用它（手动代理最优先）；
    2. `use_system_proxy` 为真 → 自动读系统代理；
       系统里没开代理时，退回 common.proxy_url（老配置里的全局地址）兜底；
    3. 其余情况直连。

    默认值：国内平台直连，e-hentai 走系统代理（见 config.DEFAULT_SETTINGS）。
    """
    settings = settings if isinstance(settings, dict) else {}
    proxy_url = str(settings.get("proxy_url") or "").strip()
    if proxy_url:
        return proxy_url, "手动代理"
    if bool(settings.get("use_system_proxy")):
        detected = detect_system_proxy()
        if detected:
            return detected, "系统代理"
        fallback = str(getattr(common, "proxy_url", "") or "").strip() if common else ""
        if fallback:
            return fallback, "备用代理（没检测到系统代理）"
        return "", "直连（没检测到系统代理）"
    return "", "直连"


def lookup_exit_ip(http: "HttpClient") -> tuple[str, str]:
    """查这个 http 客户端（走它自己配的代理）的出口 IP 与两位国家/地区码。

    返回 (ip, country)，country 形如 "HK" / "JP"；查不到时抛 HttpError。
    """
    last_error: Optional[Exception] = None
    for url in IP_LOOKUP_URLS:
        try:
            data = http.get_json(url, retry=False, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 换下一个查询源
            last_error = exc
            continue
        if not isinstance(data, dict):
            continue
        ip = str(data.get("ip") or data.get("query") or "").strip()
        country = str(data.get("country_code") or data.get("country") or "").strip()
        country = country.upper() if len(country) == 2 else ""
        if ip or country:
            return ip, country
    raise HttpError(f"查不到出口 IP：{last_error}")


class HttpClient:
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        cookies: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        retries: int = 3,
        dump_dir: Optional[Path] = None,
        log_prefix: str = "http",
        extra_headers: Optional[dict[str, str]] = None,
        proxy_url: str = "",
        use_system_proxy: bool = False,
    ) -> None:
        self.log = get_logger(log_prefix)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "*/*",
            }
        )
        if extra_headers:
            self.session.headers.update(extra_headers)
        for name, value in (cookies or {}).items():
            # 配置界面上留空的字段会保存成空字符串：不要塞进 cookie jar，
            # 否则自动补全时会出现 “buvid4=; buvid4=xxx” 这种重复下发。
            if str(value if value is not None else "").strip():
                self.session.cookies.set(name, str(value))
        effective_proxy = proxy_url.strip() if proxy_url else ""
        if not effective_proxy and use_system_proxy:
            effective_proxy = detect_system_proxy()
        if effective_proxy:
            self.session.proxies.update(
                {"http": effective_proxy, "https": effective_proxy}
            )
            self.log.info("使用代理：%s", effective_proxy)
        # 只用程序算出的代理（平台级/全局），忽略环境变量里的意外代理
        self.session.trust_env = False
        self.last_request: Optional[requests.Response] = None

    # ---------- 基础请求 ----------

    def request(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = True,
        max_wait: float = 30.0,
        retry: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Optional[Exception] = None
        attempts = (self.retries + 1) if retry else 1
        request_timeout = kwargs.pop("timeout", None) or max(self.timeout, 1.0)
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    timeout=request_timeout,
                    allow_redirects=allow_redirects,
                    **kwargs,
                )
                self.last_request = resp
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
                    wait = min(2 ** attempt, max_wait)
                    self.log.warning("%s %s 状态 %s，%s 秒后重试", method, url, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    self._dump(resp)
                    raise HttpError(
                        f"请求失败 {resp.status_code}：{method} {url}\n响应摘要：{resp.text[:300]}"
                    )
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                if attempt < attempts:
                    wait = min(2 ** (attempt - 1), max_wait)
                    self.log.warning("网络错误（%s），%s 秒后重试", exc.__class__.__name__, wait)
                    time.sleep(wait)
                    continue
            except requests.exceptions.TooManyRedirects as exc:
                raise HttpError(
                    f"{method} {url} 重定向次数过多（可能未登录、被要求验证或风控）：{exc}"
                ) from exc
        raise HttpError(f"{method} {url} 多次重试后仍然失败：{last_error}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> dict:
        resp = self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"接口返回的不是 JSON：{url}\n{resp.text[:300]}") from exc

    # ---------- 工具 ----------

    def cookie(self, name: str) -> Optional[str]:
        return self.session.cookies.get(name)

    def _dump(self, resp: requests.Response, tag: str = "error") -> None:
        if not self.dump_dir:
            return
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.dump_dir / f"{tag}-{stamp}-{hash(resp.url) % 10000:04d}.html"
        try:
            request = getattr(resp, "request", None)
            method = (request.method if request is not None else None) or "?"
            req_url = (request.url if request is not None else None) or resp.url
            header = f"<!-- debug: {method} {req_url} status={resp.status_code} -->\n"
            path.write_text(
                header + resp.text[:499_000],
                encoding="utf-8",
                errors="replace",
            )
            self.log.info(
                "已把失败响应保存到 %s（%s %s，HTTP %s）",
                path,
                method,
                req_url,
                resp.status_code,
            )
        except OSError as exc:  # pragma: no cover
            self.log.debug("保存响应失败：%s", exc)

    def close(self) -> None:
        self.session.close()
