"""Verify HTTP contracts using real storage and Agent execution with a scripted model."""

from concurrent.futures import ThreadPoolExecutor
import json
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
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import Thread


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
    self.assertEqual(updated.json()["info"]["preferences"], {"报告形式": "简短"})
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
        # read_file 工具暂时停用；上传、索引和下载仍在下方验证。
        # {"id": "plan", "name": "read_file", "args": {"artifact_id": artifact["id"]}},
      ]),
      AIMessage(content="已读旧会话，继续核对现金流。"),
      AIMessage(content="上一轮安排是核对现金流。"),
    ]
    self.model.i = 0
    continued = self.client.post(second_url + "/messages", json={"content": "读取旧会话，继续研究"})
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

  def test_workspace_research_fields_are_optional(self):
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
        for field in ("goal", "initial_hypothesis", "scope", "observation_period"):
          self.assertEqual(created["info"][field], body.get(field, ""))
          self.assertEqual(saved["info"][field], body.get(field, ""))

  def test_research_updates_preserve_omitted_fields_and_legacy_route(self):
    updated = self.client.put(self.url + "/research", json={
      "goal": "新的目标", "initial_hypothesis": "新的假设",
      "scope": "白酒行业", "observation_period": "未来一年",
    })
    self.assertEqual(updated.status_code, 200, updated.text)
    self.assertEqual(updated.json()["info"]["preferences"], self.workspace["info"]["preferences"])
    updated = self.client.put(self.url + "/preferences", json={
      "scope": "", "observation_period": "", "preferences": {},
    })
    self.assertEqual(updated.status_code, 200, updated.text)
    saved = updated.json()
    self.assertEqual(saved["info"]["goal"], "新的目标")
    self.assertEqual(saved["info"]["initial_hypothesis"], "新的假设")
    self.assertEqual(saved["info"]["scope"], "")
    self.assertEqual(saved["info"]["observation_period"], "")
    self.assertEqual(saved["info"]["preferences"], {})
    path = self.root / "data" / "workspaces" / saved["id"] / "research.json"
    for key, value in json.loads(path.read_text(encoding="utf-8")).items():
      self.assertEqual(saved["info"][key], value)
    self.assertEqual(self.client.get("/api/workspaces").json()[0]["goal"], "新的目标")
    for body in ({"goal": None}, {"scope": 12}, {"title": "不支持"}, {"preferences": None}):
      response = self.client.put(self.url + "/research", json=body)
      self.assertEqual(response.status_code, 422, response.text)
    self.assertEqual(self.client.get(self.url).json()["info"]["goal"], "新的目标")
    self.assertEqual(self.client.put("/api/workspaces/missing/research", json={"goal": "新目标"}).status_code, 404)

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

  def test_upstream_failure_marks_failed_and_releases_capacity(self):
    error = APIConnectionError(
        request=httpx.Request("POST", "http://model.invalid"),
    )
    questions = ["失败提问一", "失败提问二"]

    with patch.object(ApiTestModel, "_generate", side_effect=error):
      for index, question in enumerate(questions, start=1):
        with self.assertLogs("corpcom.api", level="ERROR"):
          response = self.client.post(
              self.thread_url + "/messages",
              json={"content": question},
          )
        self.assertEqual(response.status_code, 502, response.text)

        response = self.client.get(self.thread_url)
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()

        self.assertEqual(detail["execution_status"], "failed")
        self.assertIsNotNone(detail["execution_started_at"])
        self.assertIsNotNone(detail["execution_finished_at"])
        self.assertEqual(detail["execution_error"], str(error))
        self.assertEqual(
            [(m["role"], m["content"]) for m in detail["messages"]],
            [("user", q) for q in questions[:index]],
        )

    # 退出 patch，下一轮恢复正常模型调用。
    response = self.client.post(
        self.thread_url + "/messages",
        json={"content": "继续"},
    )
    self.assertEqual(response.status_code, 201, response.text)
    answer = response.json()

    response = self.client.get(self.thread_url)
    self.assertEqual(response.status_code, 200, response.text)
    detail = response.json()

    self.assertEqual(detail["execution_status"], "succeeded")
    self.assertIsNone(detail["execution_error"])
    self.assertIsNotNone(detail["execution_finished_at"])
    self.assertEqual(detail["messages"][-1], answer)
    self.assertEqual(
        [(m["role"], m["content"]) for m in detail["messages"]],
        [
          ("user", "失败提问一"),
          ("user", "失败提问二"),
          ("user", "继续"),
          ("assistant", "先核对现金流。"),
        ],
    )

  def test_overlapping_thread_turn_returns_409_without_duplicate_message(self):
    entered = Event()
    release = Event()

    def slow_ask(*args, **kwargs):
      entered.set()
      if not release.wait(10):
        raise TimeoutError("Test did not release the model request")
      return ask_workspace(*args, **kwargs)

    with patch("corpcom.run_coordinator.ask_workspace", side_effect=slow_ask):
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

  def test_different_threads_in_same_workspace_can_run_concurrently(self):
    response = self.client.post(
        self.url + "/threads",
        json={"title": "并行讨论"},
        )
    self.assertEqual(response.status_code, 201, response.text)
    second_thread = response.json()
    second_url = self.url + f"/threads/{second_thread['id']}"

    entered = {
      self.thread["id"]: Event(),
      second_thread["id"]: Event(),
    }
    release = Event()

    def slow_ask(store, model, workspace_id, thread_id, question,
        checkpointer, *, on_event):
      entered[thread_id].set()
      if not release.wait(15):
        raise TimeoutError("Test did not release the Agent")
      return ask_workspace(
          store, model, workspace_id, thread_id, question,
          checkpointer, on_event=on_event,
      )

    requests = [
      (self.thread_url, "分析现金流"),
      (second_url, "分析净利润"),
    ]

    with patch(
        "corpcom.run_coordinator.ask_workspace",
        side_effect=slow_ask,
    ):
      with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
          executor.submit(
              self.client.post,
              url + "/messages",
              json={"content": question},
              )
          for url, question in requests
        ]
        try:
          # 第一轮尚未释放时，第二轮也必须进入 Agent 调用入口。
          for event in entered.values():
            self.assertTrue(event.wait(5))

          for url, _ in requests:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200,
                             response.text)
            self.assertEqual(
                response.json()["execution_status"], "running",
            )
        finally:
          # 即使断言失败，也放行工作线程，避免退出线程池时一直等待。
          release.set()

        for future in futures:
          response = future.result(timeout=10)
          self.assertEqual(response.status_code, 201,
                           response.text)

    for url, question in requests:
      response = self.client.get(url)
      self.assertEqual(response.status_code, 200, response.text)
      detail = response.json()
      self.assertEqual(detail["execution_status"], "succeeded")
      self.assertEqual(
          [(message["role"], message["content"])
           for message in detail["messages"]],
          [
            ("user", question),
            ("assistant", "先核对现金流。"),
          ],
      )

  def test_capacity_is_shared_across_workspaces_and_released(self):
    # 当前 Workspace 中准备两个 Thread，用于占满两个额度。
    response = self.client.post(
      self.url + "/threads",
      json={"title": "第二个并行会话"},
      )
    self.assertEqual(response.status_code, 201, response.text)
    second_thread = response.json()
    second_url = self.url + f"/threads/{second_thread['id']}"

    # 在另一个 Workspace 准备第三个 Thread。
    response = self.client.post(
        "/api/workspaces",
        json={"title": "另一个研究", "goal": "验证整体并发额度"},
    )
    self.assertEqual(response.status_code, 201, response.text)
    other_workspace = response.json()

    response = self.client.post(
        f"/api/workspaces/{other_workspace['id']}/threads",
        json={"title": "等待额度的会话"},
    )
    self.assertEqual(response.status_code, 201, response.text)
    third_thread = response.json()
    third_url = (
      f"/api/workspaces/{other_workspace['id']}"
      f"/threads/{third_thread['id']}"
    )

    response = self.client.get(third_url)
    self.assertEqual(response.status_code, 200, response.text)
    before = response.json()

    entered = {
      self.thread["id"]: Event(),
      second_thread["id"]: Event(),
    }
    release = Event()

    def slow_ask(store, model, workspace_id, thread_id, question, checkpointer,
      *, on_event
    ):
      entered[thread_id].set()
      if not release.wait(15):
        raise TimeoutError("Test did not release the Agent")
      return ask_workspace(
          store, model, workspace_id, thread_id, question, checkpointer,
          on_event=on_event,
      )

    with patch(
        "corpcom.run_coordinator.ask_workspace",
        side_effect=slow_ask,
    ):
      with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
          executor.submit(
              self.client.post,
              url + "/messages",
              json={"content": "占用执行额度"},
          )
          for url in (self.thread_url, second_url)
        ]
        try:
          # 确认两个额度均已被占用，并且尚未释放。
          for event in entered.values():
            self.assertTrue(event.wait(5))

          rejected = self.client.post(
              third_url + "/messages",
              json={"content": "这条问题不应保存"},
          )
          self.assertEqual(rejected.status_code, 503, rejected.text)

          response = self.client.get(third_url)
          self.assertEqual(response.status_code, 200, response.text)
          self.assertEqual(response.json(), before)
        finally:
          release.set()

        # 等待前两轮完整结束，确保额度已经释放。
        for future in futures:
          response = future.result(timeout=10)
          self.assertEqual(response.status_code, 201, response.text)

    # 已退出 patch，第三轮正常调用真实 Agent 和脚本化模型。
    response = self.client.post(
        third_url + "/messages",
        json={"content": "现在继续研究"},
    )
    self.assertEqual(response.status_code, 201, response.text)

    response = self.client.get(third_url)
    self.assertEqual(response.status_code, 200, response.text)
    detail = response.json()
    self.assertEqual(detail["execution_status"], "succeeded")
    self.assertEqual(
        [(message["role"], message["content"])
         for message in detail["messages"]],
        [
          ("user", "现在继续研究"),
          ("assistant", "先核对现金流。"),
        ],
    )

  def test_startup_marks_running_threads_interrupted(self):
    data_dir = self.root / "restart-data"

    # 准备遗留状态，此时还没有启动应用。
    with WorkspaceStore(data_dir) as store:
      workspace = store.create_workspace("重启测试")
      running = store.create_thread(workspace.id, "执行中")
      succeeded = store.create_thread(workspace.id, "已成功")
      failed = store.create_thread(workspace.id, "已失败")
      idle = store.create_thread(workspace.id, "未执行")

      for thread in (running, succeeded, failed):
        store.add_message(
            workspace.id, thread.id, "user", "测试问题",
        )
        store.start_thread_execution(workspace.id, thread.id)

      store.complete_thread_execution(
          workspace.id, succeeded.id, "已保存的回答",
      )
      store.finish_thread_execution(
          workspace.id, failed.id,
          status="failed", error="测试失败",
      )

      before = {
        thread.id: store.get_thread(
            workspace.id, thread.id,
        ).model_dump(mode="json")
        for thread in (running, succeeded, failed, idle)
      }

    # 普通打开 Store 不应改变运行状态。
    with WorkspaceStore(data_dir) as store:
      self.assertEqual(
          store.get_thread(
              workspace.id, running.id,
          ).model_dump(mode="json"),
          before[running.id],
      )

    urls = {
      thread_id: (
        f"/api/workspaces/{workspace.id}/threads/{thread_id}"
      )
      for thread_id in before
    }

    # 进入 TestClient 上下文，实际执行 lifespan。
    after = {}
    with TestClient(create_app(data_dir)) as client:
      for thread_id, url in urls.items():
        response = client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        after[thread_id] = response.json()

      interrupted = after[running.id]
      original = before[running.id]

      self.assertEqual(
          interrupted["execution_status"], "interrupted",
      )
      self.assertEqual(
          interrupted["execution_started_at"],
          original["execution_started_at"],
      )
      self.assertIsNotNone(interrupted["execution_finished_at"])
      self.assertEqual(
          interrupted["execution_error"],
          "后端重启，上次执行已中断",
      )
      self.assertEqual(
          interrupted["messages"], original["messages"],
      )

      # 成功、失败和未执行的会话都不应改变。
      for thread in (succeeded, failed, idle):
        self.assertEqual(after[thread.id], before[thread.id])

    # 再启动一次，已中断状态及其结束时间不应被刷新。
    with TestClient(create_app(data_dir)) as client:
      for thread_id, url in urls.items():
        response = client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), after[thread_id])

  def test_streaming_message_returns_events_and_saves_answer(self):
    response = self.client.post(
        self.thread_url + "/messages/stream",
        json={"content": "测试流式回答"},
    )
    self.assertEqual(response.status_code, 200, response.text)
    self.assertTrue(
        response.headers["content-type"].startswith("text/event-stream")
    )
    events = [
      json.loads(line[5:].strip())
      for line in response.text.splitlines()
      if line.startswith("data:")
    ]
    self.assertEqual(
        [event["type"] for event in events],
        ["started", "text_delta", "succeeded"]
    )
    self.assertEqual(
        events[0]["workspace_id"], self.workspace["id"]
    )
    self.assertEqual(
        events[0]["thread_id"], self.thread["id"]
    )
    self.assertEqual(
        events[1]["text"], "先核对现金流。"
    )
    response = self.client.get(self.thread_url)
    self.assertEqual(response.status_code, 200)
    detail = response.json()
    self.assertEqual(detail["execution_status"], "succeeded")
    assistant_messages = [
      msg for msg in detail["messages"] if msg["role"] == "assistant"
    ]
    self.assertEqual(len(assistant_messages), 1)
    self.assertEqual(assistant_messages[0], events[-1]["message"])

  def test_streaming_rejects_running_thread_before_sse(self):
    with WorkspaceStore(self.root / "data") as store:
      store.start_thread_execution(
          self.workspace["id"], self.thread["id"],
      )

    before = self.client.get(self.thread_url).json()

    response = self.client.post(
        self.thread_url + "/messages/stream",
        json={"content": "这条问题不应保存"},
    )
    self.assertEqual(response.status_code, 409, response.text)
    self.assertTrue(
        response.headers["content-type"].startswith("application/json")
    )
    self.assertIn("detail", response.json())
    after = self.client.get(self.thread_url).json()
    self.assertEqual(after, before)

if __name__ == "__main__":
  unittest.main()
