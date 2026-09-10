"""本地侧云端调度客户端：打包漫画目录并提交到 remote_scheduler。

CLI 用法：
    python -m manga_uploader.remote_client --server https://8.147.64.46:8972 --token xxx \
        create --dir examples/my_comic --platforms bilibili,tieba \
        --at "2026-09-08T23:30" --config config.yaml
    python -m manga_uploader.remote_client --server ... list
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .comic import find_meta_file, load_chapters, read_meta
from .util import IMAGE_EXTS, ensure_utf8


SKIP_DIRS = {".git", "__pycache__", "output", "out", "prepared", "preview", "thumbnails"}


def _endpoint(base: str) -> tuple[str, int, bool]:
    parsed = urlparse(base if "://" in base else "http://" + base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port, parsed.scheme == "https"


def _json_request(base: str, token: str, method: str, path: str, payload: Any = None, timeout: float = 60) -> dict:
    import requests

    host, port, secure = _endpoint(base)
    url = f"{'https' if secure else 'http'}://{host}:{port}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.request(
        method,
        url,
        json=payload if payload is not None else None,
        headers=headers,
        timeout=timeout,
        verify=False if secure else True,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"调度服务器返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
    if resp.status_code >= 400 or not data.get("ok", True):
        raise RuntimeError(f"调度服务器错误（HTTP {resp.status_code}）：{data.get('error') or data}")
    return data


def upload_file_raw(
    base: str,
    token: str,
    path: str,
    file_path: Path,
    timeout: float = 3600,
) -> dict:
    """PUT 整个文件流式上传（http.client，带 Content-Length，不占大量内存）。"""
    host, port, secure = _endpoint(base)
    size = file_path.stat().st_size
    context = None
    if secure:
        context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context) if secure else http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.putrequest("PUT", path)
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Type", "application/zip")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body or "{}")
        if resp.status >= 400 or not data.get("ok", True):
            raise RuntimeError(f"上传失败（HTTP {resp.status}）：{data.get('error') or body[:200]}")
        return data
    finally:
        conn.close()


def load_config_payload(config_path: Optional[str] = None) -> dict[str, Any]:
    from .web import _load_payload

    payload, note = _load_payload(config_path)
    return payload


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def validate_schedule_content(
    comic_dir: str,
    platforms: list[str],
    only_chapters: Optional[list[str]] = None,
) -> list[str]:
    """发布前内容校验：标题/简介（或作者/社团）必须真实填写，防止把空内容发出去。

    返回问题描述列表；空列表表示通过。
    """
    root = Path(comic_dir).expanduser().resolve()
    try:
        chapters = load_chapters(root, only_chapters=only_chapters, strict=False)
    except Exception:
        return []
    if not chapters:
        return []
    root_meta = read_meta(find_meta_file(root)) if find_meta_file(root) else {}
    listed_by_key = {}
    for item in root_meta.get("chapters") or []:
        if isinstance(item, dict):
            key = str(item.get("folder") or item.get("key") or item.get("name") or "")
            listed_by_key[key] = item

    def level_meta(platform: str, *level_dicts: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for level in level_dicts:
            pm = level.get("platforms") if isinstance(level.get("platforms"), dict) else {}
            item = pm.get(platform) if isinstance(pm, dict) else None
            if isinstance(item, dict):
                for key, value in item.items():
                    if not out.get(key) and str(value or "").strip():
                        out[key] = str(value).strip()
        return out

    problems: list[str] = []
    for chapter in chapters:
        folder_meta = (
            read_meta(find_meta_file(chapter.source_dir))
            if find_meta_file(chapter.source_dir)
            else {}
        )
        listed = listed_by_key.get(chapter.key) or {}
        # 平台覆盖（platforms.<平台>.title/description）优先，再按 章节目录→chapters条目→根 取非空
        for platform in platforms:
            if platform not in ("bilibili", "tieba", "xiaoheihe", "ehentai", "zaimanhua"):
                continue
            pm = level_meta(platform, folder_meta, listed, root_meta)
            title = _first_nonempty(
                pm.get("title"),
                folder_meta.get("title"),
                listed.get("title"),
                root_meta.get("title"),
                folder_meta.get("title_jp"),
                listed.get("title_jp"),
                root_meta.get("title_jp"),
                pm.get("work_name") if platform == "zaimanhua" else None,
            )
            description = _first_nonempty(
                pm.get("description"),
                pm.get("caption"),
                folder_meta.get("description"),
                listed.get("description"),
                root_meta.get("description"),
            )
            author = _first_nonempty(
                folder_meta.get("author"), listed.get("author"), root_meta.get("author")
            )
            circle = _first_nonempty(
                folder_meta.get("circle"),
                listed.get("circle"),
                root_meta.get("circle"),
                folder_meta.get("社团"),
                listed.get("社团"),
                root_meta.get("社团"),
            )
            label = {"bilibili": "B站", "tieba": "贴吧", "xiaoheihe": "小黑盒",
                     "ehentai": "e-hentai", "zaimanhua": "再漫画"}.get(platform, platform)
            chapter_name = chapter.title or chapter.key
            if not title:
                problems.append(f"{label}·{chapter_name}：缺少标题，请先在「漫画信息」填写中文标题")
            elif platform in ("bilibili", "tieba", "xiaoheihe") and not (description or author or circle):
                problems.append(
                    f"{label}·{chapter_name}：缺少简介（或作者/社团），请填写后再发布"
                )
    return problems


def package_comic(
    comic_dir: str,
    zip_path: Path,
    only_chapters: Optional[list[str]] = None,
) -> dict[str, Any]:
    """把漫画目录打成 zip（统一包在 manga/ 前缀下，保留 manga.json 等元数据）。"""
    root = Path(comic_dir).expanduser().resolve()
    chapters = load_chapters(root, only_chapters=only_chapters, strict=False)
    if not chapters:
        raise ValueError("没有可打包的章节")
    selected_dirs = {ch.source_dir.name for ch in chapters}
    include_root_files = not any(ch.source_dir != root for ch in chapters)
    meta = [
        {"key": ch.key, "title": ch.title, "pages": len(ch.pages)}
        for ch in chapters
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        root_meta_names = {
            "manga.json",
            "manga.yaml",
            "manga.yml",
            "comic.json",
            "comic.yaml",
        }
        for child in sorted(root.iterdir()):
            if child.is_file():
                if child.name.lower() in root_meta_names or include_root_files:
                    zf.write(child, f"manga/{child.name}")
                continue
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                if child.name in selected_dirs:
                    for path in sorted(child.rglob("*")):
                        if path.is_dir():
                            continue
                        rel = path.relative_to(root)
                        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                            continue
                        zf.write(path, f"manga/{rel.as_posix()}")
    return {"chapters": meta}


def schedule_job(
    *,
    server: str,
    token: str,
    comic_dir: str,
    config_payload: dict[str, Any],
    platforms: list[str],
    publish_at: Any,
    chapters: Optional[list[str]] = None,
    title: str = "",
    dry_run: bool = False,
    keep_zip: Optional[Path] = None,
    accounts: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """创建任务 → 打包上传 → 提交。返回服务器任务对象。"""
    data = _json_request(
        server,
        token,
        "POST",
        "/api/jobs",
        {
            "config": config_payload,
            "platforms": platforms,
            "chapters": chapters,
            "publish_at": publish_at,
            "title": title,
            "dry_run": bool(dry_run),
            # 创建任务那一刻的账号快照（贴吧/B站），用于界面核对
            "accounts": dict(accounts or {}),
        },
    )
    job = data["job"]
    upload_url = data["upload_url"]
    zip_path = keep_zip or Path(tempfile.mkstemp(suffix=".zip", prefix="manga_sched_")[1])
    zip_path = Path(zip_path)
    try:
        package_comic(comic_dir, zip_path, only_chapters=chapters)
        upload_file_raw(server, token, upload_url, zip_path)
        result = _json_request(server, token, "POST", f"/api/jobs/{job['id']}/commit")
        return result["job"]
    finally:
        if not keep_zip:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="漫画云端定时发布客户端")
    parser.add_argument("--server", default=os.environ.get("MANGASCHED_SERVER", "http://127.0.0.1:8972"))
    parser.add_argument("--token", default=os.environ.get("MANGASCHED_TOKEN", ""))
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("create", help="创建定时发布任务")
    p.add_argument("--dir", required=True, help="漫画根目录（子目录=各话）")
    p.add_argument("--platforms", required=True, help="逗号分隔平台名")
    p.add_argument("--at", required=True, help='发布时间，如 "2026-09-09T10:30"')
    p.add_argument("--chapters", default="", help="逗号分隔章节 key（留空=全部）")
    p.add_argument("--config", default="", help="config.yaml 路径（默认自动查找）")
    p.add_argument("--title", default="", help="备注标题（可选）")
    p.add_argument("--dry-run", action="store_true", help="只跑计划不真发布")
    p.add_argument("--keep-zip", default="", help="保留打包 zip 到该路径（调试用）")

    p = sub.add_parser("list", help="查看任务列表")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("detail", help="查看任务详情/日志")
    p.add_argument("id")

    p = sub.add_parser("cancel", help="取消任务")
    p.add_argument("id")

    p = sub.add_parser("retry", help="重试已失败任务")
    p.add_argument("id")

    p = sub.add_parser("delete", help="删除任务")
    p.add_argument("id")
    return parser


def _fmt(job: dict[str, Any]) -> str:
    counts = (job.get("result") or {}).get("counts") or {}
    return (
        f"{job['id']}  {job.get('status'):8s}  {job.get('publish_at_text','')}  "
        f"平台={','.join(job.get('platforms') or [])}  "
        f"章节={','.join(job.get('chapters') or []) or '全部'}  "
        f"结果={counts}  {job.get('title','')}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    ensure_utf8()
    args = build_parser().parse_args(argv)
    token = args.token or ""
    if not token:
        # 尝试本地 token 文件（部署在本机时）
        local = Path("sched_data/token.txt")
        if local.is_file():
            token = local.read_text(encoding="utf-8").strip()
    if not token:
        print("缺少 --token（或 MANGASCHED_TOKEN）")
        return 2
    try:
        if args.action == "create":
            platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
            chapters = [c.strip() for c in args.chapters.split(",") if c.strip()] or None
            job = schedule_job(
                server=args.server,
                token=token,
                comic_dir=args.dir,
                config_payload=load_config_payload(args.config or None),
                platforms=platforms,
                publish_at=args.at,
                chapters=chapters,
                title=args.title,
                dry_run=args.dry_run,
                keep_zip=Path(args.keep_zip) if args.keep_zip else None,
            )
            print("已创建任务：", _fmt(job))
            print("到期时间：", job.get("publish_at_text"))
            return 0
        if args.action == "list":
            data = _json_request(args.server, token, "GET", "/api/jobs")
            jobs = data.get("jobs") or []
            if not jobs:
                print("（暂无任务）")
                return 0
            for job in jobs[: max(1, args.limit)]:
                print(_fmt(job))
            return 0
        if args.action == "detail":
            data = _json_request(args.server, token, "GET", f"/api/jobs/{args.id}")
            job = data["job"]
            print(json.dumps({k: v for k, v in job.items() if k != "log_tail"}, ensure_ascii=False, indent=1))
            print("\n--- 日志尾部 ---\n" + (job.get("log_tail") or "（无）"))
            return 0
        if args.action in ("cancel", "retry", "delete"):
            path = f"/api/jobs/{args.id}"
            if args.action == "delete":
                _json_request(args.server, token, "DELETE", path)
            else:
                result = _json_request(args.server, token, "POST", path + ("/retry" if args.action == "retry" else "/cancel"))
                print("已执行：", _fmt(result["job"]))
            return 0
    except Exception as exc:
        print(f"操作失败：{exc}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
