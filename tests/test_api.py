"""Verify HTTP contracts using real storage and Agent execution with a scripted model."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from openai import APIConnectionError

from corpcom.api import create_app, get_model
from corpcom.workspace_agent import ask_workspace


class ApiTestModel(FakeMessagesListChatModel):
    """Provide model replies while leaving HTTP, tools and persistence in the execution path."""

    def bind_tools(self, tools, **kwargs):
        return self


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with patch.dict(os.environ, {"CORPCOM_CORS_ORIGINS": "http://localhost:5173"}):
            self.app = create_app(self.root / "data")
        self.model = ApiTestModel(responses=[AIMessage(content="先核对现金流。")])
        self.app.dependency_overrides[get_model] = lambda: self.model
        self.client = self.enterContext(TestClient(self.app))
        result = self.client.post("/api/workspaces", json={
            "title": "公司研究", "goal": "检验经营韧性", "initial_hypothesis": "现金流稳定",
            "preferences": {"报告语言": "中文"},
        })
        self.assertEqual(result.status_code, 201, result.text)
        self.workspace = result.json()
        self.url = f"/api/workspaces/{self.workspace['id']}"
        result = self.client.post(self.url + "/threads", json={"title": "首次讨论"})
        self.assertEqual(result.status_code, 201, result.text)
        self.thread = result.json()
        self.thread_url = self.url + f"/threads/{self.thread['id']}"

    def test_research_flow_and_restart_through_http(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/workspaces").json()[0]["id"], self.workspace["id"])
        updated = self.client.put(self.url + "/preferences", json={"preferences": {"报告形式": "简短"}})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["preferences"], {"报告形式": "简短"})
        first = self.client.post(self.thread_url + "/messages", json={"content": "先分析什么？"})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["role"], "assistant")

        uploaded = self.client.post(self.url + "/files", files={
            "file": ("plan.md", b"Check cash flow.", "text/markdown"),
        }, data={"kind": "plan"})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        artifact = uploaded.json()
        self.assertNotIn("path", artifact)
        second = self.client.post(self.url + "/threads", json={"title": "继续研究"}).json()
        second_url = self.url + f"/threads/{second['id']}"
        self.model.responses = [
            AIMessage(content="", tool_calls=[
                {"id": "history", "name": "read_thread", "args": {"thread_id": self.thread["id"]}},
                {"id": "plan", "name": "read_file", "args": {"artifact_id": artifact["id"]}},
            ]),
            AIMessage(content="已读旧会话和计划，继续核对现金流。"),
            AIMessage(content="上一轮安排是核对现金流。"),
        ]
        self.model.i = 0
        continued = self.client.post(second_url + "/messages", json={"content": "读取旧会话和计划，继续研究"})
        self.assertEqual(continued.status_code, 201, continued.text)
        followup = self.client.post(second_url + "/messages", json={"content": "刚才安排了什么？"})
        self.assertEqual(followup.status_code, 201, followup.text)

        # A fresh app reads persisted records without retaining the first app's objects.
        with TestClient(create_app(self.root / "data")) as reopened:
            messages = reopened.get(second_url).json()["messages"]
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(messages[1], continued.json())
            self.assertEqual(messages[3], followup.json())
            self.assertEqual(reopened.get(self.url).json()["artifacts"], [artifact])
            self.assertEqual(reopened.get(self.url + f"/files/{artifact['id']}").json(), artifact)
            download = reopened.get(self.url + f"/files/{artifact['id']}/content")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, b"Check cash flow.")

    def test_cross_workspace_reads_and_messages_return_404(self):
        artifact = self.client.post(self.url + "/files", files={"file": ("private.txt", b"private")}).json()
        other = self.client.post("/api/workspaces", json={
            "title": "其他研究", "goal": "其他目标", "initial_hypothesis": "待验证",
        }).json()
        other_url = f"/api/workspaces/{other['id']}"
        for suffix in (
            f"/threads/{self.thread['id']}",
            f"/files/{artifact['id']}",
            f"/files/{artifact['id']}/content",
        ):
            with self.subTest(path=suffix):
                self.assertEqual(self.client.get(other_url + suffix).status_code, 404)
        response = self.client.post(other_url + f"/threads/{self.thread['id']}/messages", json={"content": "不应写入"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(self.thread_url).json()["messages"], [])
        self.assertEqual(self.client.get("/api/workspaces/missing").status_code, 404)

    def test_workspace_goal_and_hypothesis_are_optional(self):
        for body in (
            {"title": "只填写标题"},
            {"title": "留空字段", "goal": "", "initial_hypothesis": ""},
            {"title": "只有目标", "goal": "检验现金流"},
            {"title": "只有假设", "initial_hypothesis": "现金流稳定"},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/workspaces", json=body)
                self.assertEqual(response.status_code, 201, response.text)
                created = response.json()
                saved = self.client.get(f"/api/workspaces/{created['id']}").json()
                for field in ("goal", "initial_hypothesis"):
                    self.assertEqual(created[field], body.get(field, ""))
                    self.assertEqual(saved[field], body.get(field, ""))

    def test_invalid_http_inputs_do_not_write_records(self):
        before = self.client.get(self.url).json()
        for path, body in (
            (self.url + "/threads", {"title": "  "}),
            (self.thread_url + "/messages", {"content": "\n"}),
            (self.thread_url + "/messages", {"content": "伪造回答", "role": "assistant"}),
            ("/api/workspaces", {}),
            ("/api/workspaces", {"title": "  "}),
        ):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.client.post(path, json=body).status_code, 422)
        self.assertEqual(self.client.put(self.url + "/preferences", json={"preferences": []}).status_code, 422)
        invalid_number = self.client.put(
            self.url + "/preferences", content='{"preferences":{"x":NaN}}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(invalid_number.status_code, 422)
        self.assertEqual(self.client.post(
            self.url + "/files", files={"file": ("../escape.txt", b"invalid")},
        ).status_code, 422)
        self.assertEqual(self.client.get(self.url).json(), before)

    def test_upstream_failure_returns_502_and_releases_workspace(self):
        error = APIConnectionError(request=httpx.Request("POST", "http://model.invalid"))
        with patch.object(ApiTestModel, "_generate", side_effect=error):
            with self.assertLogs("corpcom.api", level="ERROR"):
                failed = self.client.post(self.thread_url + "/messages", json={"content": "失败的提问"})
        self.assertEqual(failed.status_code, 502)
        history = self.client.get(self.thread_url).json()["messages"]
        self.assertEqual([(item["role"], item["content"]) for item in history], [("user", "失败的提问")])
        next_turn = self.client.post(self.thread_url + "/messages", json={"content": "继续"})
        self.assertEqual(next_turn.status_code, 201, next_turn.text)

    def test_overlapping_workspace_turn_returns_409_without_duplicate_message(self):
        entered = Event()
        release = Event()

        def slow_ask(*args):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the model request")
            return ask_workspace(*args)

        with patch("corpcom.api.ask_workspace", side_effect=slow_ask):
            with ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(self.client.post, self.thread_url + "/messages", json={"content": "第一条"})
                try:
                    self.assertTrue(entered.wait(5))
                    second = self.client.post(self.thread_url + "/messages", json={"content": "不应写入"})
                    self.assertEqual(second.status_code, 409)
                    # Model work in a worker must not block the HTTP event loop.
                    self.assertEqual(self.client.get("/health").status_code, 200)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10).status_code, 201)
        messages = self.client.get(self.thread_url).json()["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in messages], [
            ("user", "第一条"), ("assistant", "先核对现金流。"),
        ])

    def test_openapi_and_browser_preflight(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/workspaces/{workspace_id}/threads/{thread_id}/messages", schema["paths"])
        self.assertEqual(self.client.get("/docs").status_code, 200)
        allowed = self.client.options("/api/workspaces", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["access-control-allow-origin"], "http://localhost:5173")

    def test_unconfigured_model_keeps_storage_available(self):
        self.app.dependency_overrides.clear()
        with patch.dict(os.environ, {"CORPCOM_MODEL": "", "OPENAI_API_KEY": ""}):
            self.assertEqual(self.client.get(self.url).status_code, 200)
            result = self.client.post(self.thread_url + "/messages", json={"content": "提问"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.client.get(self.thread_url).json()["messages"], [])


if __name__ == "__main__":
    unittest.main()
