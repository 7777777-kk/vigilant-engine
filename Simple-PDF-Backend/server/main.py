from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import multiprocessing
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .engines import TOOLS, inspect_file, libreoffice_path, worker_entry

logger = logging.getLogger("simple_pdf")
MAX_UPLOAD = 20 * 1024 * 1024
TTL = int(os.getenv("PDF_TTL_SECONDS", "3600"))
JOB_TIMEOUT = int(os.getenv("PDF_JOB_TIMEOUT", "180"))
DATA_ROOT = Path(os.getenv("PDF_DATA_DIR", str(Path(__file__).parent / "data"))).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
uploads: dict[str, dict] = {}
transfers: dict[str, dict] = {}
transfer_locks: dict[str, asyncio.Lock] = {}
jobs: dict[str, dict] = {}
tasks: set[asyncio.Task] = set()
conversion_slots: asyncio.Semaphore | None = None


def persist(directory: Path, entry: dict) -> None:
    temporary = directory / "meta.tmp"
    temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    temporary.replace(directory / "meta.json")


def remove_directory(identifier: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Invalid managed directory")
    target = (DATA_ROOT / identifier).resolve()
    if target.parent != DATA_ROOT:
        raise ValueError("Managed directory escaped root")
    if target.exists():
        shutil.rmtree(target)


def restore_registry() -> None:
    uploads.clear()
    transfers.clear()
    transfer_locks.clear()
    jobs.clear()
    for directory in DATA_ROOT.iterdir():
        if not directory.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            continue
        try:
            entry = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            if entry.get("id") != directory.name or entry.get("kind") not in ("upload", "job"):
                raise ValueError("invalid record")
            if entry.get("expiresAt", 0) <= time.time():
                remove_directory(directory.name)
                continue
            if entry["kind"] == "job" and entry["status"] in ("queued", "processing"):
                entry.update(status="failed", error="服务重启导致任务中断，请重新转换。")
                persist(directory, entry)
            (uploads if entry["kind"] == "upload" else jobs)[entry["id"]] = entry
        except Exception:
            remove_directory(directory.name)


def sweep_expired() -> None:
    now = time.time()
    protected_inputs = {identifier for job in jobs.values() if job["status"] in ("queued", "processing")
                        for identifier in job["fileIds"]}
    for registry in (uploads, jobs, transfers):
        for identifier, entry in list(registry.items()):
            if entry["expiresAt"] > now or identifier in protected_inputs or entry.get('finalizing'):
                continue
            if registry is jobs and entry["status"] in ("queued", "processing"):
                continue
            remove_directory(identifier)
            registry.pop(identifier, None)
            transfer_locks.pop(identifier, None)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        sweep_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global conversion_slots
    restore_registry()
    conversion_slots = asyncio.Semaphore(2)
    cleaner = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleaner.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await cleaner
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Simple PDF API", version="1.1.0", lifespan=lifespan)


def owner(x_client_token: Annotated[str, Header()] = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", x_client_token):
        raise HTTPException(401, "会话凭证无效，请重新连接服务。")
    return hashlib.sha256(x_client_token.encode()).hexdigest()


def owned(registry: dict, identifier: str, digest: str) -> dict:
    entry = registry.get(identifier)
    if not entry or not hmac.compare_digest(entry["owner"], digest) or entry["expiresAt"] <= time.time():
        raise HTTPException(404, "文件或任务已过期，请重新选择文件。")
    return entry


def public_upload(entry: dict) -> dict:
    return {key: entry[key] for key in ("id", "name", "size", "format", "pages", "hasText", "expiresAt")}


def public_job(entry: dict) -> dict:
    result = {key: entry[key] for key in ("id", "tool", "title", "status", "stage", "createdAt", "expiresAt")}
    result["error"] = entry.get("error", "")
    if entry["status"] == "done":
        result["warning"] = entry["result"]["warning"]
        result["pages"] = entry["result"]["pages"]
        result["previewPath"] = f"/api/jobs/{entry['id']}/preview" if entry["result"]["hasPreview"] else ""
        result["files"] = [{key: file[key] for key in ("id", "name", "size", "format")} | {
            "downloadPath": f"/api/jobs/{entry['id']}/files/{file['id']}"
        } for file in entry["result"]["files"]]
    return result


@app.post("/api/session")
def create_session():
    return {"token": secrets.token_urlsafe(32)}


@app.get("/api/health")
def health():
    capabilities = []
    for key, tool in TOOLS.items():
        enabled = key != "word-pdf" or libreoffice_path() is not None
        capabilities.append({"id": key, **tool, "enabled": enabled,
                             "reason": "" if enabled else "服务器需要安装 LibreOffice"})
    return {"ok": True, "name": "Simple PDF", "maxUploadBytes": MAX_UPLOAD,
            "cloudTransfer": {"version": 1, "chunkBytes": 256 * 1024},
            "maxPages": 100, "retentionMinutes": TTL // 60, "tools": capabilities}


@app.post("/api/uploads", status_code=201)
async def upload_file(file: Annotated[UploadFile, File()],
                      fileName: Annotated[str, Form()] = "",
                      x_client_token: Annotated[str, Header()] = ""):
    digest = owner(x_client_token)
    sweep_expired()
    source_entries = list(uploads.values()) + list(transfers.values())
    active = sum(entry["size"] for entry in source_entries if entry["owner"] == digest)
    used = sum(entry["size"] for entry in source_entries) + sum(
        sum(file["size"] for file in job.get("result", {}).get("files", [])) for job in jobs.values())
    if active >= 100 * 1024 * 1024 or used >= 1024 * 1024 * 1024:
        raise HTTPException(429, "暂存空间已满，请稍后再试。")
    name = re.split(r"[/\\]", fileName or file.filename or "document")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:160]
    extension = Path(name).suffix.lower().lstrip(".")
    if extension not in ("pdf", "docx", "jpg", "jpeg", "png"):
        await file.close()
        raise HTTPException(415, "仅支持 PDF、DOCX、JPG 和 PNG。")
    identifier = uuid.uuid4().hex
    directory = DATA_ROOT / identifier
    directory.mkdir()
    path = directory / f"source.{extension}"
    size = 0
    try:
        with path.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "单个文件不能超过 20 MB。")
                if active + size > 100 * 1024 * 1024:
                    raise HTTPException(429, "当前暂存文件超过 100 MB，请稍后再试。")
                stream.write(chunk)
        if size == 0:
            raise HTTPException(422, "文件内容为空。")
        metadata = await asyncio.to_thread(inspect_file, path, extension)
        entry = {"kind": "upload", "id": identifier, "owner": digest, "name": name, "size": size,
                 "format": extension, **metadata, "createdAt": time.time(), "expiresAt": time.time() + TTL}
        persist(directory, entry)
        uploads[identifier] = entry
        return public_upload(entry)
    except ValueError as exc:
        remove_directory(identifier)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        remove_directory(identifier)
        raise
    finally:
        await file.close()


class JobOptions(BaseModel):
    pages: str = Field(default="", max_length=600)
    imageFormat: Literal["png", "jpg"] = "png"
    dpi: Literal[96, 144, 200] = 144


class JobRequest(BaseModel):
    tool: str = Field(max_length=30)
    fileIds: list[str] = Field(min_length=1, max_length=12)
    options: JobOptions = Field(default_factory=JobOptions)


def run_worker(tool: str, inputs: list[dict], options: dict, output_dir: str) -> dict:
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=worker_entry, args=(send, tool, inputs, options, output_dir))
    process.start()
    send.close()
    try:
        if receive.poll(JOB_TIMEOUT):
            try:
                payload = receive.recv()
            except EOFError:
                raise ValueError("转换进程异常退出，请更换文件重试。")
            if not payload["ok"]:
                raise ValueError(payload["error"])
            return payload["result"]
        raise ValueError("转换超时，请减少页数或降低清晰度后重试。")
    finally:
        receive.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        process.close()


async def process_job(entry: dict, inputs: list[dict], options: dict) -> None:
    assert conversion_slots is not None
    async with conversion_slots:
        entry.update(status="processing", stage="正在转换文件")
        persist(DATA_ROOT / entry["id"], entry)
        try:
            result = await asyncio.to_thread(run_worker, entry["tool"], inputs, options,
                                            str(DATA_ROOT / entry["id"]))
            entry.update(status="done", stage="转换完成", result=result, expiresAt=time.time() + TTL)
        except Exception as exc:
            logger.warning("Conversion failed (%s): %s", entry["id"], type(exc).__name__)
            error = str(exc) if isinstance(exc, ValueError) else "转换失败，请稍后重试。"
            entry.update(status="failed", stage="转换失败", error=error)
        persist(DATA_ROOT / entry["id"], entry)


@app.post("/api/jobs", status_code=202)
async def create_job(request: JobRequest, x_client_token: Annotated[str, Header()] = ""):
    digest = owner(x_client_token)
    tool = TOOLS.get(request.tool)
    if not tool:
        raise HTTPException(422, "不支持的转换功能。")
    if request.tool == "word-pdf" and not libreoffice_path():
        raise HTTPException(503, "服务器尚未安装 LibreOffice，Word 转 PDF 暂不可用。")
    if not tool["min"] <= len(request.fileIds) <= tool["max"]:
        raise HTTPException(422, f"此功能需要 {tool['min']}–{tool['max']} 个文件。")
    if len(set(request.fileIds)) != len(request.fileIds):
        raise HTTPException(422, "不能重复添加同一个文件。")
    if any(job["owner"] == digest and job["status"] in ("queued", "processing") for job in jobs.values()):
        raise HTTPException(409, "已有转换任务正在运行，请等待完成。")
    if sum(job["status"] in ("queued", "processing") for job in jobs.values()) >= 8:
        raise HTTPException(429, "转换队列已满，请稍后重试。")
    inputs = []
    for identifier in request.fileIds:
        entry = owned(uploads, identifier, digest)
        if entry["format"] not in tool["accept"]:
            raise HTTPException(422, "所选文件格式与转换功能不匹配。")
        path = DATA_ROOT / identifier / f"source.{entry['format']}"
        inputs.append({"path": str(path), "name": entry["name"]})
    identifier = uuid.uuid4().hex
    entry = {"kind": "job", "id": identifier, "owner": digest, "tool": request.tool,
             "title": tool["title"], "status": "queued", "stage": "等待转换",
             "createdAt": time.time(), "expiresAt": time.time() + TTL, "fileIds": request.fileIds}
    (DATA_ROOT / identifier).mkdir()
    persist(DATA_ROOT / identifier, entry)
    jobs[identifier] = entry
    task = asyncio.create_task(process_job(entry, inputs, request.options.model_dump()))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return public_job(entry)


@app.get("/api/jobs/{identifier}")
def get_job(identifier: str, x_client_token: Annotated[str, Header()] = ""):
    return public_job(owned(jobs, identifier, owner(x_client_token)))


@app.get("/api/jobs/{identifier}/files/{file_id}")
def download_result(identifier: str, file_id: str, x_client_token: Annotated[str, Header()] = ""):
    entry = owned(jobs, identifier, owner(x_client_token))
    if entry["status"] != "done":
        raise HTTPException(409, "结果尚未生成。")
    file = next((item for item in entry["result"]["files"] if item["id"] == file_id), None)
    if not file:
        raise HTTPException(404, "结果文件不存在。")
    path = DATA_ROOT / identifier / file["disk"]
    if not path.is_file():
        raise HTTPException(404, "结果文件已清理。")
    media = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "png": "image/png", "jpg": "image/jpeg", "txt": "text/plain; charset=utf-8", "zip": "application/zip"}
    return FileResponse(path, filename=file["name"], media_type=media.get(file["format"], "application/octet-stream"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{identifier}/preview")
def preview_result(identifier: str, x_client_token: Annotated[str, Header()] = ""):
    entry = owned(jobs, identifier, owner(x_client_token))
    path = DATA_ROOT / identifier / "preview.png"
    if entry["status"] != "done" or not path.is_file():
        raise HTTPException(404, "暂无缩略图，请直接打开文档。")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.delete("/api/jobs/{identifier}", status_code=204)
async def delete_job(identifier: str, x_client_token: Annotated[str, Header()] = ""):
    entry = owned(jobs, identifier, owner(x_client_token))
    if entry["status"] in ("queued", "processing"):
        raise HTTPException(409, "任务正在运行，暂不能删除。")
    remove_directory(identifier)
    jobs.pop(identifier)


@app.delete("/api/uploads/{identifier}", status_code=204)
async def delete_upload(identifier: str, x_client_token: Annotated[str, Header()] = ""):
    owned(uploads, identifier, owner(x_client_token))
    if any(identifier in job["fileIds"] and job["status"] in ("queued", "processing") for job in jobs.values()):
        raise HTTPException(409, "文件正在转换，暂不能删除。")
    remove_directory(identifier)
    uploads.pop(identifier)


from .cloud_transfer import install as install_cloud_transfer
install_cloud_transfer(app, globals())
