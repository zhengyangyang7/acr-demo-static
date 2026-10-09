# -*- coding: utf-8 -*-
"""周度更新网页：上传源文件/目标文件，调用可替换的更新函数，下载产出文件。"""
import importlib.util
import os
import re
import shutil
import threading
import traceback
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

RunUpdate = Callable[[str, str, Callable[[str], None], bool], bool]
RunSplit = Callable[[str, Callable[..., None]], Tuple[str, int]]

_tool_mod = None


def _load_tool():
    global _tool_mod
    if _tool_mod is None:
        tool = ROOT / "周度数据更新工具.py"
        spec = importlib.util.spec_from_file_location("zhoudu_tool", tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _tool_mod = mod
    return _tool_mod


def _default_run_update(source_path, target_path, log_func, keep_other_team):
    return _load_tool().process_file(
        source_path, target_path, log_func=log_func, keep_other_team=keep_other_team
    )


def _default_run_split(file_path, log_func):
    return _load_tool().split_kaohe_by_advisor(file_path, log_func=log_func)


def _date_key(filename: str) -> str:
    m = re.search(r"截止(\d{8})", filename)
    return m.group(1) if m else "00000000"


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "")
    name = name.replace("\\", "_").replace("/", "_")
    return name or "upload.bin"


class JobStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.kind = "update"
        self.logs: List[str] = []
        self.output_path: Optional[str] = None
        self.error: Optional[str] = None

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "kind": self.kind,
                "logs": list(self.logs),
                "has_output": bool(self.output_path and os.path.isfile(self.output_path)),
                "output_name": os.path.basename(self.output_path) if self.output_path else None,
                "error": self.error,
            }

    def log(self, text: str):
        with self.lock:
            self.logs.append(str(text))

    def set_running(self, kind="update"):
        with self.lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.kind = kind
            self.logs = []
            self.output_path = None
            self.error = None
            return True

    def finish(self, ok: bool, output_path: Optional[str], error: Optional[str] = None):
        with self.lock:
            self.status = "success" if ok else "error"
            self.output_path = output_path
            self.error = error


def _zip_dir(src_dir: str, zip_path: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, src_dir))


def create_app(
    run_update: Optional[RunUpdate] = None,
    run_split: Optional[RunSplit] = None,
    work_dir: Optional[str] = None,
) -> FastAPI:
    runner = run_update or _default_run_update
    splitter = run_split or _default_run_split
    base_dir = Path(work_dir) if work_dir else Path(os.environ.get("ZHOUDU_WORK_DIR", ROOT / "web_work"))
    job = JobStore()

    app = FastAPI(title="周度数据更新")
    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def _incoming() -> Path:
        return base_dir / "incoming"

    def _reset_work():
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)
        _incoming().mkdir(parents=True, exist_ok=True)

    def _find_output(target_path: str) -> Optional[str]:
        folder = os.path.dirname(target_path)
        if not os.path.isdir(folder):
            return None
        found = []
        for name in os.listdir(folder):
            if "_更新_" in name and name.lower().endswith((".xlsx", ".xlsm")):
                found.append(os.path.join(folder, name))
        if not found:
            return None
        found.sort(key=lambda p: os.path.getmtime(p))
        return found[-1]

    def _worker(source_paths: List[str], target_path: str, keep_other_team: bool):
        last_out = None
        try:
            for src in source_paths:
                job.log(f"处理: {os.path.basename(src)}")
                ok = runner(src, target_path, job.log, keep_other_team)
                last_out = _find_output(target_path) or last_out
                if not ok:
                    job.finish(False, last_out, "更新未完成")
                    return
            if not last_out:
                job.finish(False, None, "未生成产出文件")
                return
            job.finish(True, last_out)
        except Exception as e:
            job.log(traceback.format_exc())
            job.finish(False, last_out, str(e))

    @app.get("/")
    def home():
        index = STATIC / "index.html"
        if not index.exists():
            return JSONResponse({"error": "缺少页面"}, status_code=500)
        return FileResponse(index)

    @app.get("/api/jobs/current")
    def current_job():
        return job.snapshot()

    @app.get("/api/jobs/current/download")
    def download():
        snap = job.snapshot()
        path = None
        with job.lock:
            path = job.output_path
        if snap["status"] != "success" or not path or not os.path.isfile(path):
            return JSONResponse({"error": "没有可下载的产出文件"}, status_code=404)
        filename = os.path.basename(path)
        if filename.lower().endswith(".zip"):
            media = "application/zip"
        else:
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return FileResponse(
            path,
            filename=filename,
            media_type=media,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    @app.post("/api/jobs")
    async def start_job(request: Request):
        if not job.set_running("update"):
            return JSONResponse({"error": "已有作业在进行中"}, status_code=409)
        form = await request.form()
        keep_other_team = str(form.get("keep_other_team") or "true")
        source_list = [
            item for item in form.getlist("sources")
            if isinstance(item, UploadFile) and item.filename
        ]
        target = form.get("target")
        if not source_list:
            job.finish(False, None, "请上传源文件")
            return JSONResponse({"error": "请上传源文件"}, status_code=400)
        if not isinstance(target, UploadFile) or not target.filename:
            job.finish(False, None, "请上传目标文件")
            return JSONResponse({"error": "请上传目标文件"}, status_code=400)

        _reset_work()
        incoming = _incoming()
        saved_sources = []
        for item in source_list:
            name = _safe_name(item.filename)
            dest = incoming / name
            dest.write_bytes(await item.read())
            saved_sources.append(str(dest))
        target_name = _safe_name(target.filename)
        target_path = str(incoming / target_name)
        Path(target_path).write_bytes(await target.read())
        saved_sources.sort(key=lambda p: _date_key(os.path.basename(p)))
        keep = str(keep_other_team).lower() not in ("0", "false", "no", "off")

        threading.Thread(
            target=_worker,
            args=(saved_sources, target_path, keep),
            daemon=True,
        ).start()
        return {"status": "running"}

    def _split_worker(file_path: str):
        try:
            def log(msg, tag=None):
                job.log(str(msg))

            out_dir, count = splitter(file_path, log)
            zip_path = str(base_dir / (os.path.basename(out_dir.rstrip("\\/")) + ".zip"))
            _zip_dir(out_dir, zip_path)
            job.log(f"已打包 {count} 个文件，可下载 zip")
            job.finish(True, zip_path)
        except Exception as e:
            job.log(traceback.format_exc())
            job.finish(False, None, str(e))

    @app.post("/api/split")
    async def start_split(request: Request):
        if not job.set_running("split"):
            return JSONResponse({"error": "已有作业在进行中"}, status_code=409)
        form = await request.form()
        source = form.get("source")
        if not isinstance(source, UploadFile) or not source.filename:
            job.finish(False, None, "请上传要拆分的源文件")
            return JSONResponse({"error": "请上传要拆分的源文件"}, status_code=400)
        _reset_work()
        incoming = _incoming()
        dest = incoming / _safe_name(source.filename)
        dest.write_bytes(await source.read())
        threading.Thread(
            target=_split_worker,
            args=(str(dest),),
            daemon=True,
        ).start()
        return {"status": "running"}

    return app
