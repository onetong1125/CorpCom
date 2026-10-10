"""Expose workspace storage and Agent conversations through a local HTTP API."""

import logging
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from openai import APIError
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from corpcom.workspace import RecordNotFoundError, WorkspaceStore
from corpcom.workspace_agent import ask_workspace
from corpcom.workspace_models import (
    Artifact, Message, Thread, ThreadDetail, Workspace, WorkspaceDetail, WorkspaceSummary,
)


logger = logging.getLogger(__name__)
RequiredText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    title: RequiredText
    goal: RequiredText
    initial_hypothesis: RequiredText
    scope: str = ""
    observation_period: str = ""
    preferences: dict[str, JsonValue] = Field(default_factory=dict)


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    preferences: dict[str, JsonValue]


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: RequiredText


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: RequiredText


def get_model() -> BaseChatModel:
    """Configure the model only for chat requests, keeping storage usable without credentials."""
    if not os.getenv("CORPCOM_MODEL") or not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "Model is not configured")
    return ChatOpenAI(
        model=os.environ["CORPCOM_MODEL"],
        base_url=os.getenv("OPENAI_BASE_URL"),
        temperature=0,
    )


def create_app(data_dir: Path | None = None) -> FastAPI:
    """Build a single-worker local app; tests can select a separate data directory."""
    load_dotenv()
    root = (data_dir if data_dir is not None else Path(os.getenv("CORPCOM_DATA_DIR", "data"))).resolve()
    app = FastAPI(title="CorpCom", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.getenv(
            "CORPCOM_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",") if origin.strip()],
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )
    active_workspaces: set[str] = set()
    active_lock = Lock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Invalid values such as NaN cannot themselves be encoded in a JSON error response.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]})

    @app.exception_handler(RecordNotFoundError)
    async def record_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def file_not_found(request, exc):
        logger.exception("Stored file is missing", exc_info=exc)
        return JSONResponse(status_code=404, content={"detail": "Stored file is missing"})

    # Sync routes own their SQLite connections in the same worker that uses them.
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/workspaces", status_code=201)
    def create_workspace(body: WorkspaceCreate) -> Workspace:
        with WorkspaceStore(root) as store:
            return store.create_workspace(**body.model_dump())

    @app.get("/api/workspaces")
    def list_workspaces() -> list[WorkspaceSummary]:
        with WorkspaceStore(root) as store:
            return store.list_workspaces()

    @app.get("/api/workspaces/{workspace_id}")
    def show_workspace(workspace_id: str) -> WorkspaceDetail:
        with WorkspaceStore(root) as store:
            return store.get_workspace_detail(workspace_id)

    @app.put("/api/workspaces/{workspace_id}/preferences")
    def replace_preferences(workspace_id: str, body: PreferencesUpdate) -> Workspace:
        with WorkspaceStore(root) as store:
            return store.set_preferences(workspace_id, body.preferences)

    @app.post("/api/workspaces/{workspace_id}/threads", status_code=201)
    def create_thread(workspace_id: str, body: ThreadCreate) -> Thread:
        with WorkspaceStore(root) as store:
            return store.create_thread(workspace_id, body.title)

    @app.get("/api/workspaces/{workspace_id}/threads/{thread_id}")
    def show_thread(workspace_id: str, thread_id: str) -> ThreadDetail:
        with WorkspaceStore(root) as store:
            return store.get_thread(workspace_id, thread_id)

    @app.post("/api/workspaces/{workspace_id}/threads/{thread_id}/messages", status_code=201)
    def send_message(
        workspace_id: str, thread_id: str, body: MessageCreate,
        model: Annotated[BaseChatModel, Depends(get_model)],
    ) -> Message:
        with WorkspaceStore(root) as store:
            store.get_thread(workspace_id, thread_id)
            # Reject overlapping turns before persisting a new user message.
            with active_lock:
                if workspace_id in active_workspaces:
                    raise HTTPException(409, "A conversation is already running in this workspace")
                active_workspaces.add(workspace_id)
            try:
                ask_workspace(store, model, workspace_id, thread_id, body.content)
                return store.get_thread(workspace_id, thread_id).messages[-1]
            except APIError as exc:
                logger.exception("Model request failed")
                raise HTTPException(502, "Model request failed; the user message was saved") from exc
            except ModelCallLimitExceededError as exc:
                logger.exception("Agent exceeded its model call limit")
                raise HTTPException(502, "Agent exceeded its model call limit; the user message was saved") from exc
            finally:
                with active_lock:
                    active_workspaces.remove(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/files", status_code=201, response_model=Artifact)
    def upload_file(
        workspace_id: str, file: UploadFile,
        kind: Annotated[str, Form(min_length=1, pattern=r"\S")] = "file",
    ) -> Artifact:
        filename = file.filename
        # Multipart filenames are user input; never interpret them as local paths.
        if not filename or not filename.strip() or filename in (".", "..") or any(
            char in filename for char in ("/", "\\", "\x00")
        ):
            raise HTTPException(422, "File must have a plain filename without path components")
        with WorkspaceStore(root) as store:
            store.get_workspace(workspace_id)
            with TemporaryDirectory() as temporary:
                source = Path(temporary) / filename
                with source.open("wb") as destination:
                    shutil.copyfileobj(file.file, destination)
                return store.import_file(workspace_id, source, kind)

    @app.get("/api/workspaces/{workspace_id}/files/{artifact_id}", response_model=Artifact)
    def show_file(workspace_id: str, artifact_id: str) -> Artifact:
        with WorkspaceStore(root) as store:
            return store.get_file(workspace_id, artifact_id)

    @app.get("/api/workspaces/{workspace_id}/files/{artifact_id}/content")
    def download_file(workspace_id: str, artifact_id: str) -> FileResponse:
        with WorkspaceStore(root) as store:
            file = store.get_file(workspace_id, artifact_id)
        return FileResponse(file.path, filename=file.name, media_type="application/octet-stream")

    return app
