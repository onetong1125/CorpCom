import { afterEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";
import { useResearchRuns } from "../src/useResearchRuns";
import { applyRunEvent } from "../src/researchRunState";
import { displayedMessages, conversationEntries } from "../src/conversation";
import { controlledStream } from "./stream-fixtures";

let wrapper;
afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("keeps backend execution state while reconnecting, then applies a terminal replay", async () => {
  vi.useFakeTimers();
  const initial = controlledStream();
  const replay = controlledStream();
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(initial.response)
    .mockResolvedValueOnce(replay.response));
  const detail = { id: "t1", messages: [], execution_status: null };
  let research;
  wrapper = mount({
    setup() {
      research = useResearchRuns({
        workspace: ref(null), thread: ref(detail), refreshResearch: vi.fn(),
      });
      return () => null;
    },
  });
  const done = research.send("w1", detail, "问题");
  expect(research.currentRun.value.executionStatus).toBeNull();
  expect(research.currentRun.value.connectionStatus).toBe("connecting");
  initial.emit({ type: "started", workspace_id: "w1", thread_id: "t1", run_id: "r1" }, "1-0");
  await flushPromises();
  initial.close();
  await flushPromises();
  expect(research.currentRun.value.executionStatus).toBe("running");
  expect(research.currentRun.value.connectionStatus).toBe("reconnecting");
  await vi.advanceTimersByTimeAsync(1000);
  replay.emit({ type: "run_finished", run_id: "r1", status: "failed", error: "模型失败" });
  await done;
  expect(research.currentRun.value.executionStatus).toBe("failed");
  expect(research.currentRun.value.connectionStatus).toBe("closed");
  expect(research.executionError.value).toBe("模型失败");
  expect(fetch.mock.calls.map(([url, options]) => [url, options.method || "GET"])).toEqual([
    ["/api/workspaces/w1/threads/t1/messages/stream", "POST"],
    ["/api/workspaces/w1/threads/t1/runs/r1/events?after=1-0", "GET"],
  ]);
});

it("keeps execution state after a delivery error and reconciles it from saved messages", async () => {
  const initial = controlledStream();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(initial.response));
  const detail = { id: "t1", messages: [], execution_status: null };
  let research;
  wrapper = mount({
    setup() {
      research = useResearchRuns({
        workspace: ref(null), thread: ref(detail), refreshResearch: vi.fn(),
      });
      return () => null;
    },
  });
  const done = research.send("w1", detail, "问题");
  initial.emit({ type: "started", workspace_id: "w1", thread_id: "t1", run_id: "r1" });
  initial.emit({ type: "unknown" });
  await done;
  expect(research.currentRun.value.executionStatus).toBe("running");
  expect(research.currentRun.value.connectionStatus).toBe("disconnected");
  research.reconcileThread({
    id: "t1", execution_status: "succeeded", execution_error: null,
    messages: [{ id: 1, role: "user", content: "问题" }],
  });
  expect(research.currentRun.value.executionStatus).toBe("succeeded");
  expect(research.currentRun.value.connectionStatus).toBe("disconnected");
  expect(research.executionError.value).toBe("");
});

it("projects streamed and restored tool exchanges into the same conversation order", () => {
  const question = { id: 1, role: "user", content: "问题" };
  const plan = { id: 2, role: "assistant", model_message_id: "plan", content: "先查询" };
  const answer = { id: 3, role: "assistant", model_message_id: "answer", content: "结论" };
  const run = { userMessageId: 1, userMessageCount: 0, items: [], error: "" };
  for (const event of [
    { type: "text_delta", message_id: "plan", text: "先查询" },
    { type: "tool_start", tool_run_id: "callback", tool_call_id: "call", name: "lookup" },
    { type: "tool_end", tool_run_id: "callback", tool_call_id: "call", status: "success", content: "结果" },
    { type: "text_delta", message_id: "answer", text: "结论" },
    { type: "succeeded", message: answer },
  ]) applyRunEvent("w1", "t1", run, event);
  const history = { userMessageId: 1, items: [], error: "" };
  for (const event of [
    { type: "assistant_message", message_id: "plan", text: "先查询", tool_calls: [{ id: "call", name: "lookup", args: {} }] },
    { type: "tool_result", tool_call_id: "call", status: "success", content: "结果" },
    { type: "assistant_message", message_id: "answer", text: "结论", tool_calls: [] },
  ]) applyRunEvent("w1", "t1", history, event);
  const streamed = conversationEntries(displayedMessages({ messages: [question, plan] }, run), "", [run], run);
  const restored = conversationEntries([question, plan, answer], "", [history], history);
  for (const entries of [streamed, restored]) {
    expect(entries.map((entry) => entry.type === "message" ? entry.message.content : entry.items[0].content))
      .toEqual(["问题", "先查询", "结果", "结论"]);
    expect(entries[2].items[0].status).toBe("returned");
  }
});
