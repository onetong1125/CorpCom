"""Expose workspace storage and Agent conversations through a local HTTP API."""

import logging
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Annotated
from contextlib import asynccontextmanager
from anyio import create_task_group
from redis import Redis

from dotenv import load_dotenv
from collections.abc import AsyncIterator
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from openai import APIError
from pydantic import BaseModel, ConfigDict, Field

from corpcom.run_events import RunEventStream
from corpcom.workspace import RecordNotFoundError, WorkspaceStore, ThreadBusyError
from corpcom.workspace_models import (
    Artifact, Message, Thread, ThreadDetail, Workspace, WorkspaceDetail, WorkspaceResearchInfo, WorkspaceSummary,
)
from corpcom.run_coordinator import RunCoordinator, ExecutionCapacityError
from corpcom.workspace_stream import research_events

logger = logging.getLogger(__name__)
RequiredText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class WorkspaceCreate(WorkspaceResearchInfo):
    title: RequiredText


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_dotenv()
    root = (data_dir if data_dir is not None else Path(os.getenv("CORPCOM_DATA_DIR", "data"))).resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        root.mkdir(parents=True, exist_ok=True)
        # Keep the checkpoint connection open for the application's lifetime.
        with (
            Redis.from_url(
                os.environ["CORPCOM_REDIS_URL"],
                decode_responses=True,
            ) as redis_client,
            SqliteSaver.from_conn_string(
            str(root / "checkpoints.sqlite")
        ) as checkpointer,
        ):
            app.state.redis = redis_client
            app.state.checkpointer = checkpointer
            app.state.coordinator = RunCoordinator(
                checkpointer, redis_client, max_concurrent_runs=2)
            with WorkspaceStore(root) as store:
                store.interrupt_running_executions()
            async with create_task_group() as task_group:
                app.state.research_tasks = task_group
                yield

    app = FastAPI(
        title="CorpCom",
        version="0.1.0",
        lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.getenv(
            "CORPCOM_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",") if origin.strip()],
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )

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
    @app.put("/api/workspaces/{workspace_id}/research")
    def update_research(workspace_id: str, body: WorkspaceResearchInfo) -> Workspace:
        with WorkspaceStore(root) as store:
            return store.update_research(workspace_id, body)

    @app.post("/api/workspaces/{workspace_id}/threads", status_code=201)
    def create_thread(workspace_id: str, body: ThreadCreate) -> Thread:
        with WorkspaceStore(root) as store:
            return store.create_thread(workspace_id, body.title)

    @app.get("/api/workspaces/{workspace_id}/threads/{thread_id}")
    def show_thread(workspace_id: str, thread_id: str) -> ThreadDetail:
        with WorkspaceStore(root) as store:
            return store.get_thread(workspace_id, thread_id)

    async def get_research_events(
        workspace_id: str, thread_id: str, body: MessageCreate,
        model: Annotated[BaseChatModel, Depends(get_model)]
    ):
        """统一提供应用对象，并转换HTTPException"""
        try:
            async with research_events(
                root,
                app.state.coordinator,
                app.state.research_tasks,
                workspace_id,
                thread_id,
                body.content,
                model
            ) as events:
                yield events
        except ThreadBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ExecutionCapacityError as exc:
            raise HTTPException(503, str(exc)) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/workspaces/{workspace_id}/threads/{thread_id}/messages/stream",
              response_class=EventSourceResponse
    )
    async def stream_message(
        events: Annotated[
            AsyncIterator[dict[str, object]],
            Depends(get_research_events, scope="request")
        ]
    ):
        # 这里已经准入成功，进入SSE
        async for event in events:
            payload = event.copy()
            event_id = payload.pop("event_id", None)
            yield ServerSentEvent(data=payload, id=event_id)

    @app.get(
        "/api/workspaces/{workspace_id}/threads/{thread_id}"
        "/runs/{run_id}/events"
    )
    def read_run_events(
        workspace_id: str,
        thread_id: str,
        run_id: str,
        after: Annotated[
            str, Query(pattern=r"^[0-9]+-[0-9]+$")
        ] = "0-0",
    ) -> list[dict[str, object]]:
        with WorkspaceStore(root) as store:
            store.get_research_run(workspace_id, thread_id, run_id)

        stream = RunEventStream(app.state.redis, run_id)
        return [
            {"id": event_id, "data": event}
            for event_id, event in stream.read_after(after)
        ]

    @app.get(
        "/api/workspaces/{workspace_id}/threads/{thread_id}"
        "/runs/{run_id}"
    )
    def read_run_status(
        workspace_id: str,
        thread_id: str,
        run_id: str,
    ) -> dict[str, object]:
        with WorkspaceStore(root) as store:
            run = store.get_research_run(
                workspace_id, thread_id, run_id,
            )

        return {
            "id": run.id,
            "status": run.status,
            "error": run.error,
        }

    @app.post("/api/workspaces/{workspace_id}/threads/{thread_id}/messages", status_code=201)
    def send_message(
        workspace_id: str, thread_id: str, body: MessageCreate,
        model: Annotated[BaseChatModel, Depends(get_model)],
    ) -> Message:
        with WorkspaceStore(root) as store:
            store.get_thread(workspace_id, thread_id)
            # Reject overlapping turns before persisting a new user message.
            try:
                return app.state.coordinator.run(
                    store, model, workspace_id, thread_id, body.content,
                )
            except APIError as exc:
                logger.exception("Model request failed")
                raise HTTPException(502, "Model request failed; the user message was saved") from exc
            except ModelCallLimitExceededError as exc:
                logger.exception("Agent exceeded its model call limit")
                raise HTTPException(502, "Agent exceeded its model call limit; the user message was saved") from exc
            except ThreadBusyError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ExecutionCapacityError as exc:
                raise HTTPException(503, str(exc)) from exc

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
