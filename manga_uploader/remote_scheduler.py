"""云端定时调度服务（可脱离本机运行）。

职责：接收“漫画包 + 平台配置 + 发布时间”的任务，到点后在本机（云服务器）
复用 Runner 完成发布。本模块只做服务端，不包含前端。

运行：
    python -m manga_uploader.remote_scheduler \
        --host 0.0.0.0 --port 8972 --data /opt/manga-sched

鉴权：启动时若未通过 --token / MANGASCHED_TOKEN 指定，会在 data/token.txt
自动生成一个随机 token（所有 /api 请求需带 Authorization: Bearer <token>）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import ssl
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import util
from .comic import load_chapters
from .remote_client import validate_schedule_content
from .webui import build_app


MAX_BODY = 4 * 1024 * 1024 * 1024  # 单包最大 4GB
WORKER_INTERVAL = 5.0
CLEANUP_DELAY = 600.0  # 任务结束后先保留 10 分钟（可看日志/结果），再清大文件
# 允许早于服务器时间这么多秒的“立即发布”；再早就是选错时间，直接报错
IMMEDIATE_GRACE = 120.0
# 计划时间距现在不足这么多秒时，任务列表里标成“立即发布”，避免用户以为定错了
IMMEDIATE_NOTICE = 60.0


def keep_seconds() -> float:
    """完成/取消任务彻底删除（含日志记录）前的保留时长，默认 24 小时。"""
    try:
        hours = float(os.environ.get("MANGASCHED_KEEP_HOURS", "24"))
    except (TypeError, ValueError):
        hours = 24.0
    return max(0.0, hours * 3600.0)


def now_epoch() -> float:
    return time.time()


def parse_publish_at(value: Any) -> float:
    """接受 epoch 秒 / ISO 字符串；无时区的按北京时间(UTC+8)解释。"""
    if value in (None, ""):
        raise ValueError("缺少发布时间 publish_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("发布时间为空")
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析发布时间：{value}") from exc
    if dt.tzinfo is None:
        # 网页 datetime-local 默认本地时间（中国时区 UTC+8）
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.timestamp()


def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def fmt_time_tz(epoch: float) -> str:
    """带时区偏移的时间文本。

    服务器与用户本机时区可能不同（“我明明设的 10 点”这类误会大多源于此），
    所以给用户看的时间一律带上 UTC 偏移。
    """
    text = datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    offset = datetime.fromtimestamp(epoch).astimezone().strftime("%z")
    if offset:
        return f"{text} UTC{offset[:3]}:{offset[3:]}"
    return text


class JobStore:
    """jobs.json + jobs/<id>/ 的持久化任务仓库（线程安全）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.jobs_dir = data_dir / "jobs"
        self.store_file = data_dir / "jobs.json"
        self.lock = threading.RLock()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        if self.store_file.exists():
            try:
                raw = json.loads(self.store_file.read_text(encoding="utf-8"))
                self._jobs = raw.get("jobs") if isinstance(raw, dict) else {}
            except (OSError, json.JSONDecodeError):
                self._jobs = {}
        self.flush()

    def flush(self) -> None:
        tmp = self.store_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"jobs": self._jobs}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(self.store_file)

    def _mutate(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(fields)
            job["updated_at"] = fmt_time(now_epoch())
            self.flush()
            return job

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        job_dir = self.jobs_dir / job_id
        # 先把参数校验干净再落盘：校验失败的请求不该在服务器上留下
        # 带 Cookie 的配置副本或空任务目录。
        platforms = [str(p).strip().lower() for p in (payload.get("platforms") or []) if str(p).strip()]
        if not platforms:
            raise ValueError("缺少目标平台 platforms")
        chapters = payload.get("chapters")
        if isinstance(chapters, list):
            chapters = [str(c).strip() for c in chapters if str(c).strip()] or None
        else:
            chapters = None
        publish_at = parse_publish_at(payload.get("publish_at"))
        # 发布时间已经过去的话，调度线程会“立刻”把它发出去——很多人就是这么
        # 误以为“定时没生效、直接发出去了”。这里直接拒绝，让调用方重新选时间。
        now = now_epoch()
        if publish_at < now - IMMEDIATE_GRACE:
            raise ValueError(
                f"发布时间 {fmt_time_tz(publish_at)} 已经过去（服务器当前时间 "
                f"{fmt_time_tz(now)}），请改成一个将来的时间再提交"
            )
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "config.json").write_text(
            json.dumps(payload.get("config") or {}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        job = {
            "id": job_id,
            "created_at": fmt_time(now_epoch()),
            "publish_at": publish_at,
            "publish_at_text": (
                f"{fmt_time_tz(publish_at)}（立即发布）"
                if publish_at - now <= IMMEDIATE_NOTICE
                else fmt_time_tz(publish_at)
            ),
            "status": "staging",  # staging -> ready/pending -> running -> done/failed/error/canceled
            "platforms": platforms,
            "chapters": chapters,
            "title": str(payload.get("title") or "").strip(),
            "note": str(payload.get("note") or "").strip(),
            "dry_run": bool(payload.get("dry_run")),
            "comic_dir": "",
            "chapters_meta": [],
            "updated_at": fmt_time(now_epoch()),
            "started_at": "",
            "finished_at": "",
            "result": None,
            "error": "",
        }
        with self.lock:
            self._jobs[job_id] = job
            self.flush()
        return job

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = []
            for job in self._jobs.values():
                row = dict(job)
                row.pop("config", None)
                rows.append(row)
            return sorted(rows, key=lambda j: (j.get("publish_at") or 0), reverse=True)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def save_upload(self, job_id: str, stream, length: int) -> int:
        """把客户端上传的漫画 zip 流式落盘。返回写入字节数。"""
        job_dir = self.job_dir(job_id)
        dst = job_dir / "incoming.zip"
        written = 0
        with dst.open("wb") as fh:
            while written < length:
                chunk = stream.read(min(1024 * 1024, length - written))
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
        return written

    def commit(self, job_id: str) -> dict[str, Any]:
        """解包漫画 zip，校验章节后进入待发布队列。"""
        with self.lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in ("staging", "uploaded"):
                raise ValueError(f"任务状态不允许提交：{job['status']}")
        job_dir = self.job_dir(job_id)
        archive = job_dir / "incoming.zip"
        if not archive.is_file() or archive.stat().st_size == 0:
            raise ValueError("还没有收到漫画包（先调用 upload 接口）")
        comic_root = job_dir / "comic"
        if comic_root.exists():
            shutil.rmtree(comic_root, ignore_errors=True)
        extract_dir = job_dir / "_extract"
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                # 防路径穿越
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                target = extract_dir / name
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        # 顶层只有一层目录时去掉外壳（对应“整个漫画目录”打包的情况）
        comic_root = unwrap_single_dir(extract_dir)
        comic_root.mkdir(parents=True, exist_ok=True)
        # 校验章节
        try:
            chapters = load_chapters(
                comic_root,
                only_chapters=job["chapters"],
                strict=False,
            )
        except Exception as exc:
            self._mutate(job_id, status="error", error=f"漫画包校验失败：{exc}")
            raise ValueError(f"漫画包校验失败：{exc}") from exc
        problems = validate_schedule_content(
            str(comic_root), job["platforms"], job["chapters"]
        )
        if problems:
            message = "内容校验失败：" + "；".join(problems)
            self._mutate(job_id, status="error", error=message)
            raise ValueError(message)
        meta = [
            {"key": ch.key, "title": ch.title, "pages": len(ch.pages)}
            for ch in chapters
        ]
        job = self._mutate(
            job_id,
            status="pending",
            comic_dir=str(comic_root),
            chapters_meta=meta,
            error="",
        )
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] == "running":
                raise ValueError("任务正在执行，不能取消")
            if job["status"] in ("done", "failed", "error", "canceled"):
                return job
        return self._mutate(job_id, status="canceled", finished_at=fmt_time(now_epoch()))

    def delete(self, job_id: str) -> None:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return
            if job["status"] == "running":
                raise ValueError("任务正在执行，不能删除")
            self._jobs.pop(job_id, None)
            self.flush()
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not job.get("comic_dir") or not Path(job["comic_dir"]).is_dir():
            raise ValueError("任务缺少漫画内容，无法重试")
        return self._mutate(
            job_id,
            status="pending",
            publish_at=now_epoch(),
            publish_at_text="立即发布",
            started_at="",
            finished_at="",
            result=None,
            error="",
        )

    def log_tail(self, job_id: str, limit: int = 4000) -> str:
        path = self.job_dir(job_id) / "log.txt"
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def payload_clean(self, job_id: str) -> None:
        """任务结束后清理占空间/敏感内容，只保留 log.txt 与 report.json。"""
        job_dir = self.job_dir(job_id)
        targets = ["incoming.zip", "config.json", "output", "_extract", "comic"]
        for name in targets:
            target = job_dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass


def unwrap_single_dir(root: Path) -> Path:
    """若 root 里只有一个子目录且 root 本身无图片，返回该子目录。"""
    dirs = [p for p in root.iterdir() if p.is_dir()]
    images = [p for p in root.iterdir() if util.is_image(p)]
    if not images and len(dirs) == 1:
        return dirs[0]
    return root


def execute_job(store: JobStore, job_id: str) -> None:
    """执行单个到点任务（worker 单线程调用）。"""
    job = store.get(job_id)
    if not job:
        return
    if job["status"] != "running":
        return
    job_dir = store.job_dir(job_id)
    log_file = job_dir / "log.txt"
    # 每次执行从干净日志开始
    log_file.write_text("", encoding="utf-8")
    # 只让本任务的日志进任务 log.txt
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    util.setup_logging(log_file=log_file)
    try:
        config_path = job_dir / "config.json"
        payload = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        common = payload.setdefault("common", {})
        common["output_dir"] = str(job_dir / "output")
        app = build_app(payload)
        # 任务里显式指定的平台强制启用（调度时用户已确认目标平台）
        for name in job["platforms"]:
            if name in app.platforms:
                app.platforms[name].enabled = True
        # 云端 E 站任务强制走本机代理（mihomo 127.0.0.1:7890），
        # 避免把本机用户配置里的代理地址误用到云服务器上
        proxy_url = os.environ.get("MANGASCHED_EHENTAI_PROXY", "").strip()
        if (
            "ehentai" in job["platforms"]
            and proxy_url
            and "ehentai" in app.platforms
        ):
            ehentai = app.platforms["ehentai"]
            ehentai.settings["proxy_url"] = proxy_url
            ehentai.settings["use_system_proxy"] = False
        # dry_run 走纯计划路径，不发网络请求
        app.common.dry_run = bool(common.get("dry_run") or job.get("dry_run"))
        runner = _make_runner(app)
        planned = float(job.get("publish_at") or 0)
        delay = max(0.0, now_epoch() - planned) if planned else 0.0
        logging.getLogger(util.LOGGER_NAME).info(
            "开始执行定时任务 %s：计划 %s，到点后 %.1fs 开始；平台=%s 章节=%s",
            job_id,
            fmt_time_tz(planned) if planned else "—",
            delay,
            ",".join(job["platforms"]),
            ",".join(job["chapters"]) if job["chapters"] else "全部",
        )
        # 到点前再校验一次：没有标题/简介的任务直接失败，绝不发空内容
        problems = validate_schedule_content(
            job["comic_dir"], job["platforms"], job["chapters"]
        )
        if problems:
            raise ValueError("发布前内容校验失败：" + "；".join(problems))
        results = runner.run_publish(
            job["comic_dir"],
            names=job["platforms"],
            only_chapters=job["chapters"],
            confirm=False,
        )
        summary = summarize(results)
        report_file = job_dir / "report.json"
        report_file.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        counts = summary.get("counts") or {}
        if job.get("dry_run") or not results:
            summary["note"] = (
                "dry_run：仅生成计划，未真正发布" if job.get("dry_run") else "未产生发布结果"
            )
        status = "done" if (counts.get("failed") or 0) == 0 else "failed"
        store._mutate(
            job_id,
            status=status,
            finished_at=fmt_time(now_epoch()),
            result=summary,
            error="",
        )
        logging.getLogger(util.LOGGER_NAME).info("任务完成：%s", status)
    except Exception as exc:
        logging.getLogger(util.LOGGER_NAME).exception("任务执行异常")
        store._mutate(
            job_id,
            status="error",
            finished_at=fmt_time(now_epoch()),
            error=f"{exc}\n{traceback.format_exc()}"[-4000:],
        )


def _make_runner(app):
    from .runner import Runner

    return Runner(app)


def summarize(results) -> dict[str, Any]:
    rows = []
    counts = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        rows.append(
            {
                "platform": r.platform,
                "chapter": r.chapter,
                "title": r.title,
                "status": r.status,
                "url": r.url,
                "message": r.message,
                "pages": getattr(r, "pages", None),
            }
        )
    return {"counts": counts, "rows": rows}


class SchedulerState:
    def __init__(self, store: JobStore, token: str):
        self.store = store
        self.token = token
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                self._tick()
            except Exception:
                logging.getLogger(util.LOGGER_NAME).exception("worker tick error")
            self.stop.wait(WORKER_INTERVAL)

    def _tick(self) -> None:
        now = now_epoch()
        for job in self.store.list():
            if job["status"] != "pending":
                self._maybe_cleanup(job, now)
                continue
            if now >= float(job["publish_at"]):
                self.store._mutate(
                    job["id"],
                    status="running",
                    started_at=fmt_time(now_epoch()),
                )
                execute_job(self.store, job["id"])

    def _maybe_cleanup(self, job: dict[str, Any], now: float) -> None:
        """完成/取消的任务：先清大文件（zip/解包/Cookie 配置），到期再删整条记录。"""
        status = job["status"]
        if status not in ("done", "failed", "error", "canceled"):
            return
        full_at = job.get("cleanup_full_at")
        if full_at is None:
            self.store._mutate(
                job["id"],
                cleanup_at=now + CLEANUP_DELAY,
                cleanup_full_at=now + keep_seconds(),
            )
            return
        if now >= float(full_at):
            self.store.delete(job["id"])
            return
        cleanup_at = float(job.get("cleanup_at") or full_at)
        if now >= cleanup_at and not job.get("payload_cleaned"):
            self.store.payload_clean(job["id"])
            self.store._mutate(job["id"], payload_cleaned=True)


def auth_ok(handler: BaseHTTPRequestHandler, state: SchedulerState) -> bool:
    header = handler.headers.get("Authorization", "")
    query_token = parse_qs(urlparse(handler.path).query).get("token", [""])[0]
    given = ""
    if header.lower().startswith("bearer "):
        given = header[7:].strip()
    elif query_token:
        given = query_token.strip()
    return secrets.compare_digest(given, state.token)


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class SchedulerHandler(BaseHTTPRequestHandler):
    server_version = "manga-scheduler/1.0"

    @property
    def state(self) -> SchedulerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.getLogger(util.LOGGER_NAME).info(
            "http %s %s", self.address_string(), fmt % args
        )

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024 * 1024:
            raise ValueError("JSON 请求体大小异常")
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求体不是 JSON 对象")
        return data

    def _api(self, method: str, path: str) -> None:
        if not auth_ok(self, self.state):
            json_response(self, 401, {"error": "token 无效"})
            return
        store = self.state.store
        try:
            if method == "GET" and path == "/api/jobs":
                json_response(self, 200, {"ok": True, "jobs": store.list()})
                return
            if method == "GET" and path.startswith("/api/jobs/"):
                job_id = path.split("/")[-1]
                job = store.get(job_id)
                if job is None:
                    json_response(self, 404, {"error": "任务不存在"})
                    return
                job = dict(job)
                job["log_tail"] = store.log_tail(job_id)
                json_response(self, 200, {"ok": True, "job": job})
                return
            if method == "POST" and path == "/api/jobs":
                payload = self._read_json_body()
                job = store.create(payload)
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "job": job,
                        "upload_url": f"/api/jobs/{job['id']}/upload",
                    },
                )
                return
            if method == "PUT" and path.startswith("/api/jobs/"):
                job_id = path.split("/")[-2]
                job = store.get(job_id)
                if job is None:
                    json_response(self, 404, {"error": "任务不存在"})
                    return
                if job["status"] != "staging":
                    json_response(self, 409, {"error": f"任务状态不允许上传：{job['status']}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY:
                    json_response(self, 400, {"error": "上传大小异常"})
                    return
                written = store.save_upload(job_id, self.rfile, length)
                store._mutate(job_id, status="uploaded")
                json_response(
                    self,
                    200,
                    {"ok": True, "written": written, "commit_url": f"/api/jobs/{job_id}/commit"},
                )
                return
            if method == "POST" and path.endswith("/commit"):
                job_id = path.split("/")[-2]
                job = store.commit(job_id)
                json_response(self, 200, {"ok": True, "job": job})
                return
            if method == "POST" and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                job = store.cancel(job_id)
                json_response(self, 200, {"ok": True, "job": job})
                return
            if method == "POST" and path.endswith("/retry"):
                job_id = path.split("/")[-2]
                job = store.retry(job_id)
                json_response(self, 200, {"ok": True, "job": job})
                return
            if method == "DELETE" and path.startswith("/api/jobs/"):
                job_id = path.split("/")[-1]
                store.delete(job_id)
                json_response(self, 200, {"ok": True})
                return
        except KeyError:
            json_response(self, 404, {"error": "任务不存在"})
            return
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
            return
        except Exception as exc:
            logging.getLogger(util.LOGGER_NAME).exception("api error")
            json_response(self, 500, {"error": f"服务器错误：{exc}"})
            return
        json_response(self, 404, {"error": "未知接口"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            json_response(self, 200, {"ok": True, "service": "manga-scheduler"})
            return
        self._api("GET", parsed.path)

    def do_POST(self) -> None:
        self._api("POST", urlparse(self.path).path)

    def do_PUT(self) -> None:
        self._api("PUT", urlparse(self.path).path)

    def do_DELETE(self) -> None:
        self._api("DELETE", urlparse(self.path).path)


class SchedulerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, state: SchedulerState, cert: Optional[Path] = None):
        super().__init__(addr, handler)
        self.state = state
        self.cert = cert


def ensure_token(data_dir: Path, token: Optional[str]) -> str:
    token_file = data_dir / "token.txt"
    if token:
        token_file.write_text(token.strip(), encoding="utf-8")
        return token.strip()
    env = os.environ.get("MANGASCHED_TOKEN", "").strip()
    if env:
        token_file.write_text(env, encoding="utf-8")
        return env
    if token_file.is_file():
        value = token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    token_file.write_text(value, encoding="utf-8")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="漫画云端定时调度服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8972)
    parser.add_argument("--data", default="sched_data", help="任务/日志存储目录")
    parser.add_argument("--token", default="", help="API token；留空用环境变量或自动生成")
    parser.add_argument("--cert", default="", help="TLS 证书 PEM 路径（可选）")
    parser.add_argument("--key", default="", help="TLS 私钥 PEM 路径（可选）")
    args = parser.parse_args(argv)
    util.ensure_utf8()
    data_dir = Path(args.data).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(data_dir)
    token = ensure_token(data_dir, args.token or None)
    state = SchedulerState(store, token)
    cert = Path(args.cert).expanduser().resolve() if args.cert else data_dir / "cert.pem"
    key = Path(args.key).expanduser().resolve() if args.key else data_dir / "key.pem"
    use_tls = cert.is_file() and key.is_file()
    util.setup_logging(log_file=data_dir / "server.log")
    logger = logging.getLogger(util.LOGGER_NAME)
    logger.info("调度服务启动 data=%s 任务数=%d", data_dir, len(store.list()))
    logger.info("token 文件：%s（请勿泄露）", data_dir / "token.txt")
    server = SchedulerServer(
        (args.host, args.port),
        SchedulerHandler,
        state,
        cert=cert if use_tls else None,
    )
    if use_tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"
    logger.info("监听 %s://%s:%d", scheme, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
