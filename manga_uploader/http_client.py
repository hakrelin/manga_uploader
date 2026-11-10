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
    manual = str(settings.get("proxy_url") or "").strip()
    if manual:
        return manual, "手动代理"
    if bool(settings.get("use_system_proxy")):
        detected = detect_system_proxy()
        if detected:
            return detected, "系统代理"
        fallback = str(getattr(common, "proxy_url", "") or "").strip() if common else ""
        if fallback:
            return fallback, "备用代理（没检测到系统代理）"
        return "", "直连（没检测到系统代理）"
    return "", "直连"


# 走系统代理优先的平台（海外站）。国内站不在这里：默认直连，直连不通再自动退到代理。
OVERSEAS_PLATFORMS = {"ehentai"}


def resolve_proxy_candidates(
    settings: Any, common: Any = None, *, prefer_direct: bool = True
) -> list[tuple[str, str]]:
    """这个平台可以走的网络路径，按优先级排好：[(代理地址或 '', 说明), …]。

    **全自动**：不再有“手填了就只用它、不许改道”的模式。用户/老配置里留下的
    代理地址只会被当成候选项之一，哪条通就走哪条；一条都不通时按最后一条报错。

    顺序：
    - 国内站（prefer_direct）：直连 → 系统代理 → 配置里的代理 → common 里的兜底
    - 海外站（e-hentai）：系统代理 → 配置里的代理 → common 兜底 → 直连

    换路是**按请求**进行的：某条路径 SSL EOF / 连接重置 / 超时，就立刻换下一条
    重发同一个请求（不消耗重试次数），日志里会写明换成了哪条。
    """
    settings = settings if isinstance(settings, dict) else {}
    system = detect_system_proxy()
    manual = str(settings.get("proxy_url") or "").strip()
    fallback = str(getattr(common, "proxy_url", "") or "").strip() if common else ""

    routes: list[tuple[str, str]] = []
    if prefer_direct:
        routes.append(("", "直连"))
    if system:
        routes.append((system, "系统代理"))
    if manual:
        routes.append((manual, "配置的代理"))
    if fallback:
        routes.append((fallback, "备用代理"))
    if not prefer_direct:
        routes.append(("", "直连"))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for url, label in routes:
        if url in seen:
            continue
        seen.add(url)
        unique.append((url, label))
    return unique or [("", "直连")]


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
        proxy_candidates: Optional[list[tuple[str, str]]] = None,
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
        # 网络路径：一条或多条（失败自动换下一条）。老调用方式仍然有效。
        if proxy_candidates:
            self._routes: list[tuple[str, str]] = list(proxy_candidates)
        else:
            effective = proxy_url.strip() if proxy_url else ""
            if not effective and use_system_proxy:
                effective = detect_system_proxy()
            self._routes = [(effective, "代理" if effective else "直连")]
        self._route_index = 0
        self._apply_route(0)
        # 只用程序算出的代理（平台级/全局），忽略环境变量里的意外代理
        self.session.trust_env = False
        self.last_request: Optional[requests.Response] = None

    # ---------- 路由 ----------

    @property
    def active_proxy(self) -> str:
        return self._routes[self._route_index][0]

    @property
    def active_proxy_label(self) -> str:
        return self._routes[self._route_index][1]

    def _apply_route(self, index: int) -> None:
        url, label = self._routes[index]
        self.session.proxies.clear()
        if url:
            self.session.proxies.update({"http": url, "https": url})
            self.log.info("网络路径：%s %s", label, url)
        else:
            self.log.info("网络路径：%s", label)

    def _switch_route(self, exc: BaseException) -> bool:
        """失败就换下一条网络路径（直连↔系统代理），用户不用自己改设置。"""
        next_index = self._route_index + 1
        if next_index >= len(self._routes):
            return False
        self._route_index = next_index
        self._apply_route(next_index)
        target = self.active_proxy or "直连"
        self.log.warning(
            "当前网络路径不可用（%s：%s），自动改用%s%s 重试",
            exc.__class__.__name__,
            str(exc)[:120],
            self.active_proxy_label,
            f" {target}" if target else "",
        )
        return True

    def ensure_route(self, probe_url: str, *, timeout: Optional[float] = None) -> str:
        """用一次轻量请求确认哪条路径能通，返回最终生效的代理地址（空=直连）。

        上传大文件前先探一下，比传到一半再失败划算得多（COS 上传不走这里，
        所以小黑盒会先调它再传图）。拿到任何 HTTP 响应都算“能通”。
        """
        for _ in range(len(self._routes)):
            try:
                resp = self.session.get(
                    probe_url,
                    timeout=timeout or min(5.0, max(self.timeout, 2.0)),
                    allow_redirects=False,
                    stream=True,
                )
                resp.close()
                return self.active_proxy
            except (requests.exceptions.RequestException, OSError) as exc:
                if not self._switch_route(exc):
                    self.log.warning("所有网络路径都探测失败（最后一次：%s）", exc)
                    break
        return self.active_proxy

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
        attempt = 1
        switched = 0
        while attempt <= attempts:
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
                    attempt += 1
                    continue
                if resp.status_code >= 400:
                    self._dump(resp)
                    raise HttpError(
                        f"请求失败 {resp.status_code}：{method} {url}\n响应摘要：{resp.text[:300]}"
                    )
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                # 先试“换一条网络路径”（直连被梯子/防火墙掐断时最有用的那步）
                if switched < len(self._routes) - 1 and self._switch_route(exc):
                    switched += 1
                    continue
                if attempt < attempts:
                    wait = min(2 ** (attempt - 1), max_wait)
                    self.log.warning("网络错误（%s），%s 秒后重试", exc.__class__.__name__, wait)
                    time.sleep(wait)
                    attempt += 1
                    continue
            except requests.exceptions.TooManyRedirects as exc:
                raise HttpError(
                    f"{method} {url} 重定向次数过多（可能未登录、被要求验证或风控）：{exc}"
                ) from exc
            attempt += 1
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
