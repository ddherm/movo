"""FastAPI entry point for the single-project local service."""

from __future__ import annotations

import io
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from backend.core.service import Coordinator
from backend.core.store import StateError


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
_configured_data_dir = Path(os.getenv("VOICE_COMPANION_DATA_DIR", "data")).expanduser()
DATA_DIR = (_configured_data_dir if _configured_data_dir.is_absolute() else ROOT / _configured_data_dir).resolve()
coordinator = Coordinator(DATA_DIR, assistant_root=ROOT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await coordinator.start()
    try:
        yield
    finally:
        await coordinator.stop()


app = FastAPI(title="Movo", version="0.1.0", lifespan=lifespan)


class ProjectCreate(BaseModel):
    idea: str = Field(min_length=1, max_length=4000)
    parent_dir: str | None = Field(default=None, max_length=2000)
    folder_name: str | None = Field(default=None, max_length=80)


class ProjectSelect(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    channel: Literal["text", "voice"] = "text"


class DecisionAnswer(BaseModel):
    option_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)
    channel: Literal["text", "voice"] = "text"


class SpecChange(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str = Field(default="", max_length=2000)


@app.exception_handler(StateError)
async def state_error_handler(_request, exc: StateError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.get("/api/health")
async def health():
    return {"ok": True, "phase": (coordinator.snapshot() or {}).get("phase")}


@app.get("/api/config/status")
async def config_status():
    # Booleans only; keys and exact values never reach the browser.
    return {
        "deepseek": bool(os.getenv("DEEPSEEK_API_KEY")),
        "minimax": bool(os.getenv("MINIMAX_API_KEY") and os.getenv("MINIMAX_GROUP_ID")),
        "search": bool(os.getenv("TAVILY_API_KEY")),
        "ntfy": bool(os.getenv("NTFY_TOPIC")),
        "public_project_url": bool(os.getenv("PUBLIC_PROJECT_URL")),
    }


@app.get("/api/project")
async def project():
    return coordinator.snapshot()


@app.get("/api/projects")
async def projects():
    return {"projects": coordinator.list_projects(), "active_project_id": coordinator.store.active_project_id()}


@app.post("/api/projects/select")
async def select_project(body: ProjectSelect):
    return await coordinator.select_project(body.project_id)


@app.get("/api/folders")
async def project_folders(path: str | None = None):
    return coordinator.folders.browse(path)


@app.post("/api/project", status_code=201)
async def create_project(body: ProjectCreate):
    return await coordinator.create_project(body.idea, body.parent_dir, body.folder_name)


@app.post("/api/assets")
async def add_asset(file: UploadFile = File(...)):
    before = coordinator.snapshot()
    if before is None:
        raise StateError("请先创建项目。")
    if before["phase"] == "WAITING_DECISION":
        raise StateError("请先回答当前待决策问题，再补充参考截图。")
    allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(415, "只支持 PNG、JPEG 或 WebP 截图。")
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "截图不能超过 5 MB。")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > 25_000_000:
                raise HTTPException(413, "截图像素过大，请缩小后再上传。")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(415, "图片文件无法识别。") from None
    asset_file = f"{uuid.uuid4().hex}{allowed[file.content_type]}"
    relative = Path("uploads") / asset_file
    path = DATA_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    try:
        asset_id = coordinator.store.add_asset(Path(file.filename or "参考截图").name[:200], file.content_type, str(relative))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if before["phase"] not in {"IDEA", "CLARIFY", "RESEARCH"}:
        await coordinator.plan_current_spec()
    return {"id": asset_id, "url": f"/api/assets/{asset_id}"}


@app.get("/api/assets/{asset_id}")
async def get_asset(asset_id: str):
    relative = coordinator.store.asset_path(asset_id)
    if not relative:
        raise HTTPException(404, "截图不存在。")
    path = (DATA_DIR / relative).resolve()
    if not path.is_relative_to(DATA_DIR) or not path.is_file():
        raise HTTPException(404, "截图不存在。")
    return FileResponse(path)


@app.post("/api/messages")
async def add_message(body: MessageCreate):
    return await coordinator.message(body.text, body.channel)


@app.post("/api/voice/utterance")
async def voice_utterance(body: MessageCreate):
    return await coordinator.message(body.text, "voice")


@app.get("/api/progress")
async def progress():
    snapshot = coordinator.snapshot()
    return {"text": coordinator.progress_text(), "progress": snapshot.get("progress") if snapshot else None, "project": snapshot}


@app.post("/api/plan/approve")
async def approve_plan():
    return await coordinator.approve_plan()


@app.post("/api/decisions/{question_id}/answer")
async def answer_decision(question_id: str, body: DecisionAnswer):
    return await coordinator.answer(question_id, body.option_id, body.reason, body.channel)


@app.post("/api/changes")
async def change_spec(body: SpecChange):
    return await coordinator.change(body.text, body.reason, trigger_plan=True)


@app.post("/api/build/resume")
async def resume_build():
    return await coordinator.resume()


@app.post("/api/build/reverify")
async def reverify_saved_build():
    return await coordinator.reverify_saved_build()


@app.get("/api/voice/config")
async def voice_config() -> dict[str, Any]:
    try:
        from backend.integrations import voice_status
        return voice_status()
    except ImportError:
        return {"enabled": False, "reason": "Pipecat 语音组件尚未安装。"}


async def _voice_utterance(project_id: str, text: str) -> str:
    snapshot = coordinator.snapshot()
    if not snapshot or snapshot["id"] != project_id:
        return "项目已变化，请刷新页面后重新进入通话。"
    previous_assistant_ids = {
        item["id"] for item in snapshot["messages"] if item["role"] == "assistant"
    }
    updated = await coordinator.message(text, "voice")
    if updated["id"] != project_id:
        return "项目已变化，请刷新页面后重新进入通话。"
    new_replies = [
        item for item in updated["messages"]
        if item["role"] == "assistant" and item["id"] not in previous_assistant_ids
    ]
    return new_replies[-1]["text"] if new_replies else "已收到，我正在处理这句话。"


try:
    from backend.integrations import create_voice_router

    app.include_router(create_voice_router(_voice_utterance), prefix="/api")
except ImportError:
    pass


@app.get("/preview/")
@app.get("/preview/{path:path}")
async def preview(path: str = ""):
    snapshot = coordinator.snapshot()
    if not snapshot or not snapshot["preview_url"]:
        raise HTTPException(404, "预览尚未准备好。")
    root = (Path(snapshot["source_dir"]) / "dist").resolve()
    target = (root / (path or "index.html")).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(404, "预览资源不存在。")
    if not target.is_file():
        if Path(path).suffix or path.startswith("assets/"):
            raise HTTPException(404, "预览资源不存在。")
        # Client-side routes in the generated app use the static index.
        target = root / "index.html"
    if not target.is_file():
        raise HTTPException(404, "预览产物不存在。")
    return FileResponse(target)


frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.is_dir():
    assets_dir = frontend_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/sw.js")
    async def service_worker():
        worker = frontend_dist / "sw.js"
        if not worker.is_file():
            raise HTTPException(404)
        return FileResponse(worker, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

    @app.get("/")
    @app.get("/{route:path}")
    async def frontend(route: str = ""):
        if route.startswith("api/") or route.startswith("preview/"):
            raise HTTPException(404)
        return FileResponse(frontend_dist / "index.html")
