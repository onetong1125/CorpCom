import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import App from "../src/App.vue";
import ArtifactPanel from "../src/components/ArtifactPanel.vue";
import { successResponse, controlledStream } from "./stream-fixtures";

let wrapper;
let spaces;
let threads;
let writes;
let writeHandler;
let readHandler;

function response(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status, headers: { "Content-Type": "application/json" },
  });
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function message(id, role, content, modelMessageId = role === "assistant" ? `model-${id}` : null) {
  return { id, role, content, model_message_id: modelMessageId, created_at: "2026-11-10T00:00:00Z" };
}

beforeEach(() => {
  window.history.replaceState(null, "", "/?workspace=w1&thread=a");
  Element.prototype.scrollIntoView = vi.fn();
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  threads = Object.fromEntries(["a", "b", "c"].map((id) => [id, {
    id, workspace_id: id === "c" ? "w2" : "w1", title: `会话 ${id}`,
    execution_status: null, execution_error: null, messages: [],
  }]));
  spaces = ["w1", "w2"].map((id) => ({
    id, title: `研究 ${id}`, artifacts: [],
    info: { goal: "测试研究", initial_hypothesis: "", scope: "", observation_period: "", preferences: {} },
  }));
  writes = [];
  writeHandler = () => { throw new Error("Unexpected write"); };
  readHandler = () => undefined;
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    if (options.method) {
      writes.push({ url, body: options.body });
      return writeHandler(url, options);
    }
    const intercepted = readHandler(url);
    if (intercepted) return intercepted;
    if (url.endsWith("/runs")) return response([]);
    if (/\/threads\/[^/]+\/history$/.test(url)) return response({ thread_id: url.split("/").at(-2), turns: [] });
    if (url === "/api/workspaces") return response(spaces);
    const space = spaces.find((item) => url === `/api/workspaces/${item.id}`);
    if (space) return response({ ...space, threads: Object.values(threads)
      .filter((item) => item.workspace_id === space.id)
      .map(({ messages, ...item }) => item) });
    const thread = Object.values(threads).find((item) =>
      url === `/api/workspaces/${item.workspace_id}/threads/${item.id}`);
    if (thread) return response(thread);
    throw new Error(`Unexpected read: ${url}`);
  }));
});

afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function openApp() {
  wrapper = mount(App, { attachTo: document.body });
  await flushPromises();
}

async function send(content) {
  await wrapper.get('textarea[aria-label="研究问题"]').setValue(content);
  await wrapper.get("form.composer").trigger("submit");
  await flushPromises();
}

async function selectThread(id) {
  await wrapper.findAll(".thread-item").find((item) => item.text().includes(`会话 ${id}`)).trigger("click");
  await flushPromises();
}

async function selectWorkspace(id) {
  await wrapper.findAll(".workspace-item").find((item) => item.text().includes(`研究 ${id}`)).trigger("click");
  await flushPromises();
}

it.each([false, true])("restores legacy tools without run rows, with a newer run: %s", async (hasNewRun) => {
  threads.a.messages = [message(1, "user", "旧问题"), message(2, "assistant", "旧答案", null)];
  if (hasNewRun) threads.a.messages.push(message(3, "user", "新问题"), message(4, "assistant", "新答案", "new-answer"));
  threads.a.execution_status = "succeeded";
  readHandler = (url) => {
    if (url.endsWith("/threads/a/runs")) return response(hasNewRun ? [{ id: "new", user_message_id: 3, status: "succeeded" }] : []);
    if (url.endsWith("/threads/a/history")) return response({ thread_id: "a", turns: [
      { user_message_id: 1, events: [
        { type: "assistant_message", message_id: "old-plan", text: "先查旧资料", tool_calls: [{ id: "old-call", name: "legacy_lookup", args: { query: "旧资料" } }] },
        { type: "tool_result", tool_call_id: "old-call", content: "旧工具结果", status: "error" },
        { type: "assistant_message", message_id: "old-answer", text: "旧答案", tool_calls: [] },
      ] },
      { user_message_id: 3, events: [{ type: "assistant_message", message_id: "new-answer", text: "新答案", tool_calls: [] }] },
    ] });
    if (url.endsWith("/runs/new/history")) return response({ run: { id: "new", thread_id: "a", status: "succeeded" }, events: [
      { type: "assistant_message", message_id: "new-answer", text: "新答案", tool_calls: [] },
    ] });
  };
  await openApp();
  expect(wrapper.findAll(".tool-event")).toHaveLength(1);
  expect(wrapper.get(".tool-event").text()).toContain("旧工具结果");
  expect(wrapper.get(".tool-event").text()).toContain("执行异常");
  expect(wrapper.findAll(".research-progress").some((item) => item.text().includes("legacy_lookup"))).toBe(true);
  expect(wrapper.text().match(/旧答案/g)).toHaveLength(1);
  expect(wrapper.text().indexOf("legacy_lookup")).toBeLessThan(wrapper.text().indexOf("旧答案"));
  if (hasNewRun) expect(wrapper.text().match(/新答案/g)).toHaveLength(1);
  expect(writes).toHaveLength(0);
  await selectThread("b");
  await selectThread("a");
  expect(wrapper.findAll(".tool-event")).toHaveLength(1);
});

it("loads legacy tool history when switching to an unvisited thread", async () => {
  threads.b.messages = [message(10, "user", "旧问题 B")];
  readHandler = (url) => {
    if (url.endsWith("/threads/b/history")) return response({ thread_id: "b", turns: [
      { user_message_id: 10, events: [
        { type: "assistant_message", message_id: "b-plan", text: "", tool_calls: [{ id: "b-call", name: "lookup_b", args: {} }] },
        { type: "tool_result", tool_call_id: "b-call", status: "success", content: "B 的历史结果" },
      ] },
    ] });
  };
  await openApp();
  await selectThread("b");
  expect(wrapper.get(".tool-event").text()).toContain("B 的历史结果");
  expect(writes).toHaveLength(0);
});

it("rebuilds completed and active runs after page load without posting a question", async () => {
  const active = controlledStream();
  const replayUrls = [];
  const records = [
    { id: "old-run", run_index: 0, user_message_id: 1, status: "succeeded" },
    { id: "active-run", run_index: 1, user_message_id: 3, status: "running" },
  ];
  threads.a.messages = [message(1, "user", "旧问题"), message(2, "assistant", "旧答案", "old-answer"), message(3, "user", "新问题")];
  threads.a.execution_status = "running";
  readHandler = (url) => {
    if (url.endsWith("/threads/a/runs")) return response(records);
    if (url.includes("/runs/")) {
      replayUrls.push(url);
      if (url.endsWith("/old-run/history")) return response({
        run: { id: "old-run", thread_id: "a", status: "succeeded", error: null },
        events: [
          { type: "assistant_message", message_id: "old-plan", text: "", tool_calls: [{ id: "old-tool", name: "old_lookup", args: { query: "旧资料" } }] },
          { type: "tool_result", tool_call_id: "old-tool", content: [{ type: "text", text: "旧工具结果" }], status: "success" },
          { type: "assistant_message", message_id: "old-answer", text: "旧答案", tool_calls: [] },
        ],
      });
      return active.response;
    }
  };
  await openApp();
  await flushPromises();
  expect(replayUrls).toEqual([
    "/api/workspaces/w1/threads/a/runs/old-run/history",
    "/api/workspaces/w1/threads/a/runs/active-run/events?after=0-0",
  ]);
  active.emit({ type: "started", workspace_id: "w1", thread_id: "a", run_id: "active-run" }, "200-0");
  active.emit({ type: "text_delta", message_id: "plan", text: "正在核对" }, "200-1");
  active.emit({ type: "tool_start", tool_run_id: "current-tool", name: "new_lookup" }, "200-2");
  await flushPromises();
  expect(wrapper.text().match(/旧答案/g)).toHaveLength(1);
  expect(wrapper.findAll(".assistant-byline").map((header) =>
    header.element.nextElementSibling.className)).toEqual(["research-progress", "research-progress"]);
  expect(wrapper.text()).toContain("正在核对");
  expect(wrapper.text()).toContain("old_lookup");
  expect(wrapper.text()).toContain("旧资料");
  expect(wrapper.text()).toContain("旧工具结果");
  expect(wrapper.findAll(".tool-event").filter((item) => item.text().includes("old_lookup"))).toHaveLength(1);
  expect(wrapper.findAll(".research-progress")[0].element.open).toBe(false);
  expect(wrapper.get("textarea").element.disabled).toBe(true);
  const text = wrapper.text();
  expect(text.indexOf("old_lookup")).toBeLessThan(text.indexOf("旧答案"));
  expect(text.indexOf("旧答案")).toBeLessThan(text.indexOf("新问题"));
  await selectThread("b");
  await selectThread("a");
  expect(replayUrls).toHaveLength(2);
  threads.a.messages.push(message(4, "assistant", "正在核对", "plan"), message(5, "assistant", "核对结束", "answer"));
  threads.a.execution_status = "succeeded";
  active.emit({ type: "tool_end", tool_run_id: "current-tool" }, "200-3");
  active.emit({ type: "text_delta", message_id: "answer", text: "核对结束" }, "200-4");
  active.emit({ type: "run_finished", run_id: "active-run", status: "succeeded", error: null });
  await flushPromises();
  expect(wrapper.text().match(/正在核对/g)).toHaveLength(1);
  expect(wrapper.text().match(/核对结束/g)).toHaveLength(1);
  expect(wrapper.get("textarea").element.disabled).toBe(false);
  expect(writes).toHaveLength(0);
});

it("restores partial output and tool errors from an already failed run on reload", async () => {
  threads.a.messages = [message(1, "user", "失败的问题")];
  threads.a.execution_status = "failed";
  threads.a.execution_error = "模型失败";
  readHandler = (url) => {
    if (url.endsWith("/threads/a/runs")) return response([{ id: "failed-run", run_index: 0, user_message_id: 1, status: "failed" }]);
    if (url.endsWith("/runs/failed-run/history")) return response({
      run: { id: "failed-run", thread_id: "a", status: "failed", error: "模型失败" },
      events: [
        { type: "assistant_message", message_id: "partial", text: "未完成的计划", tool_calls: [] },
        { type: "tool_start", tool_run_id: "failed-tool", tool_call_id: "failed-call", name: "lookup" },
        { type: "tool_error", tool_run_id: "failed-tool", tool_call_id: "failed-call", error: "文件不存在" },
      ],
    });
  };
  await openApp();
  await flushPromises();
  expect(wrapper.text()).toContain("未完成的计划");
  expect(wrapper.get(".tool-event").text()).toContain("文件不存在");
  expect(wrapper.get('[role="alert"]').text()).toContain("模型失败");
  expect(wrapper.get("textarea").element.disabled).toBe(false);
  expect(writes).toHaveLength(0);
});

it("merges checkpoint tool errors and archived results into their declared cards", async () => {
  threads.a.messages = [message(1, "user", "并行查询")];
  threads.a.execution_status = "interrupted";
  readHandler = (url) => {
    if (url.endsWith("/threads/a/runs")) return response([{ id: "partial-run", user_message_id: 1, status: "interrupted" }]);
    if (url.endsWith("/partial-run/history")) return response({
      run: { id: "partial-run", thread_id: "a", status: "interrupted", error: "后端重启" },
      events: [
        { type: "assistant_message", message_id: "plan", text: "并行检索", tool_calls: [
          { id: "a", name: "lookup_a", args: { query: "A" } },
          { id: "b", name: "lookup_b", args: {} },
          { id: "c", name: "lookup_c", args: {} },
        ] },
        { type: "tool_result", tool_call_id: "a", status: "error", content: "checkpoint 工具失败" },
        { type: "tool_end", tool_run_id: "callback-b", tool_call_id: "b", status: "success", content: "归档工具结果" },
        { type: "assistant_message", message_id: "tail", text: "尚未完成", tool_calls: [] },
      ],
    });
  };
  await openApp();
  const cards = wrapper.findAll(".tool-event");
  expect(cards).toHaveLength(3);
  expect(cards[0].text()).toContain("执行异常");
  expect(cards[0].text()).toContain("checkpoint 工具失败");
  expect(cards[1].text()).toContain("已返回");
  expect(cards[1].text()).toContain("归档工具结果");
  expect(cards[2].text()).toContain("结果未收到");
  expect(wrapper.text().match(/尚未完成/g)).toHaveLength(1);
  expect(wrapper.get('[role="alert"]').text()).toContain("后端重启");
  expect(writes).toHaveLength(0);
  expect(fetch.mock.calls.some(([url]) => url.includes("/events"))).toBe(false);
});

it("shows an older history read failure without hiding the latest history", async () => {
  threads.a.messages = [message(1, "user", "旧问题"), message(2, "user", "新问题")];
  threads.a.execution_status = "succeeded";
  readHandler = (url) => {
    if (url.endsWith("/threads/a/runs")) return response([
      { id: "old", user_message_id: 1, status: "failed" },
      { id: "new", user_message_id: 2, status: "succeeded" },
    ]);
    if (url.endsWith("/old/history")) return response({ detail: "历史不存在" }, 404);
    if (url.endsWith("/new/history")) return response({
      run: { id: "new", thread_id: "a", status: "succeeded", error: null },
      events: [{ type: "assistant_message", message_id: "answer", text: "新回答", tool_calls: [] }],
    });
  };
  await openApp();
  expect(wrapper.get('[role="alert"]').text()).toContain("历史不存在");
  expect(wrapper.text()).toContain("新回答");
  expect(writes).toHaveLength(0);
  expect(fetch.mock.calls.some(([url]) => url.includes("/events"))).toBe(false);
});

it("streams text and tool events into the original thread and replaces the final increment once", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  expect(wrapper.findAll(".assistant-byline")).toHaveLength(1);
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "intro", text: "先查资料" });
  stream.emit({ type: "tool_start", tool_run_id: "tool1", name: "bash" });
  await flushPromises();
  expect(wrapper.get(".research-progress").text()).toContain("先查资料");
  expect(wrapper.get(".tool-event").text()).toContain("执行中");
  await selectThread("b");
  stream.emit({ type: "tool_end", tool_run_id: "tool1" });
  stream.emit({ type: "text_delta", message_id: "answer", text: "最终" });
  stream.emit({ type: "text_delta", message_id: "answer", text: "回答" });
  await flushPromises();
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  await selectThread("a");
  expect(wrapper.get(".research-progress").text()).toContain("最终回答");
  expect(wrapper.get(".tool-event").text()).toContain("已返回");
  threads.a.messages = [
    message(1, "user", "问题 A"),
    message(2, "assistant", "先查资料", "intro"),
    message(3, "assistant", "最终回答", "answer"),
  ];
  threads.a.execution_status = "succeeded";
  stream.emit({ type: "succeeded", message: { ...threads.a.messages[2], thread_id: "a" } });
  await flushPromises();
  const byline = wrapper.get(".assistant-byline").element;
  expect(byline.previousElementSibling).toBe(wrapper.get(".message.user").element);
  expect(byline.nextElementSibling).toBe(wrapper.findAll(".message.assistant")[0].element);
  expect(wrapper.findAll(".messages .message-byline strong").map((name) => name.text()))
    .toEqual(["你", "CorpCom"]);
  expect(wrapper.findAll(".message.assistant").map((item) => item.text())).toEqual([
    expect.stringContaining("先查资料"), expect.stringContaining("最终回答"),
  ]);
  expect(wrapper.get(".research-progress").text()).not.toContain("先查资料");
  expect(wrapper.get(".research-progress").text()).not.toContain("最终回答");
  expect(wrapper.get(".research-progress").element.open).toBe(false);
  expect(wrapper.findAll(".messages > .message, .messages > .research-progress")
    .map((item) => item.text())).toEqual([
    expect.stringContaining("问题 A"),
    expect.stringContaining("先查资料"),
    expect.stringContaining("bash"),
    expect.stringContaining("最终回答"),
  ]);
});

it("places each tool exchange after its own saved plan", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  for (const [id, text, name] of [["plan1", "读取年报", "read_report"], ["plan2", "计算比率", "calculate"]]) {
    stream.emit({ type: "text_delta", message_id: id, text });
    stream.emit({ type: "tool_start", tool_run_id: name, name });
    stream.emit({ type: "tool_end", tool_run_id: name });
  }
  stream.emit({ type: "text_delta", message_id: "answer", text: "最终结论" });
  threads.a.messages = [
    message(1, "user", "问题 A"),
    message(2, "assistant", "读取年报", "plan1"),
    message(3, "assistant", "计算比率", "plan2"),
    message(4, "assistant", "最终结论", "answer"),
  ];
  threads.a.execution_status = "succeeded";
  stream.emit({ type: "succeeded", message: { ...threads.a.messages[3], thread_id: "a" } });
  await flushPromises();
  expect(wrapper.findAll(".messages > .message, .messages > .research-progress")
    .map((item) => item.text())).toEqual([
    expect.stringContaining("问题 A"),
    expect.stringContaining("读取年报"),
    expect.stringContaining("read_report"),
    expect.stringContaining("计算比率"),
    expect.stringContaining("calculate"),
    expect.stringContaining("最终结论"),
  ]);
});

it("keeps the streamed plan and tools before the final answer when storage refresh fails", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "plan", text: "先查资料" });
  stream.emit({ type: "tool_start", tool_run_id: "t", name: "bash" });
  stream.emit({ type: "tool_end", tool_run_id: "t" });
  stream.emit({ type: "text_delta", message_id: "answer", text: "最终回答" });
  readHandler = () => Promise.reject(new Error("读回失败"));
  stream.emit({ type: "succeeded", message: { ...message(3, "assistant", "最终回答", "answer"), thread_id: "a" } });
  await flushPromises();
  expect(wrapper.findAll(".messages > .message, .messages > .research-progress")
    .map((item) => item.text())).toEqual([
    expect.stringContaining("问题 A"),
    expect.stringContaining("先查资料"),
    expect.stringContaining("最终回答"),
  ]);
  expect(wrapper.get(".research-progress").text()).toContain("bash");
  expect(wrapper.get(".research-progress").text()).not.toContain("最终回答");
});

it("keeps a later streamed answer visible when polling saves an earlier plan", async () => {
  vi.useFakeTimers();
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "plan", text: "核对资料" });
  stream.emit({ type: "tool_start", tool_run_id: "t", name: "bash" });
  stream.emit({ type: "tool_end", tool_run_id: "t" });
  // The text can match the plan; identity, not content or message count, decides replacement.
  stream.emit({ type: "text_delta", message_id: "answer", text: "核对资料" });
  threads.a.messages = [message(1, "user", "问题 A"), message(2, "assistant", "核对资料", "plan")];
  threads.a.execution_status = "running";
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.findAll(".message.assistant")).toHaveLength(1);
  expect(wrapper.get(".research-progress").text().match(/核对资料/g)).toHaveLength(1);
  expect(wrapper.get(".message.assistant").element.nextElementSibling)
    .toBe(wrapper.get(".research-progress").element);

  stream.emit({ type: "text_delta", message_id: "answer", text: "后得出结论" });
  threads.a.messages.push(message(3, "assistant", "核对资料后得出结论", "answer"));
  threads.a.execution_status = "succeeded";
  stream.emit({ type: "succeeded", message: { ...threads.a.messages[2], thread_id: "a" } });
  await flushPromises();
  expect(wrapper.findAll(".message.assistant")).toHaveLength(2);
  expect(wrapper.get(".research-progress").text()).not.toContain("核对资料");

  wrapper.unmount();
  await openApp();
  expect(wrapper.findAll(".message.assistant").map((item) => item.text())).toEqual([
    expect.stringContaining("核对资料"), expect.stringContaining("核对资料后得出结论"),
  ]);
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  expect(writes).toHaveLength(1);
});

it("restores a saved plan on reload while research is still running", async () => {
  vi.useFakeTimers();
  threads.a.execution_status = "running";
  threads.a.messages = [message(1, "user", "问题 A"), message(2, "assistant", "先查资料", "plan")];
  await openApp();
  expect(wrapper.get(".message.assistant").text()).toContain("先查资料");
  expect(wrapper.get("textarea").element.disabled).toBe(true);
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  threads.a.messages.push(message(3, "assistant", "研究结论", "answer"));
  threads.a.execution_status = "succeeded";
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.findAll(".message.assistant")).toHaveLength(2);
  expect(wrapper.get("textarea").element.disabled).toBe(false);
  expect(writes).toHaveLength(0);
});

it("retains a committed plan after failure while only unsaved text stays in progress", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "plan", text: "先查资料" });
  stream.emit({ type: "text_delta", message_id: "answer", text: "未完成结论" });
  threads.a.messages = [message(1, "user", "问题 A"), message(2, "assistant", "先查资料", "plan")];
  threads.a.execution_status = "failed";
  threads.a.execution_error = "模型连接失败";
  stream.emit({ type: "failed", error: "模型连接失败" });
  await flushPromises();
  expect(wrapper.get(".message.assistant").text()).toContain("先查资料");
  expect(wrapper.get(".research-progress").text()).not.toContain("先查资料");
  expect(wrapper.get(".research-progress").text()).toContain("未完成结论");

  wrapper.unmount();
  await openApp();
  expect(wrapper.get(".message.assistant").text()).toContain("先查资料");
  expect(wrapper.get('[role="alert"]').text()).toContain("模型连接失败");
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  expect(writes).toHaveLength(1);
});

it("keeps partial output and tool errors after a streamed failure without saving them as an answer", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "m", text: "未完成内容" });
  stream.emit({ type: "tool_start", tool_run_id: "t", name: "bash" });
  stream.emit({ type: "tool_error", tool_run_id: "t", error: "工具出错" });
  threads.a.messages = [message(1, "user", "问题 A")];
  threads.a.execution_status = "failed";
  stream.emit({ type: "failed", error: "研究出错" });
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("研究出错");
  expect(wrapper.get(".research-progress").text()).toContain("未完成内容");
  expect(wrapper.get(".tool-event").text()).toContain("工具出错");
  expect(wrapper.findAll(".message.assistant")).toHaveLength(0);
  expect(wrapper.get("textarea").element.value).toBe("问题 A");
  expect(writes).toHaveLength(1);
});

it("resumes the same run over GET after repeated disconnects using the last applied cursor", async () => {
  vi.useFakeTimers();
  const initial = controlledStream();
  const firstReplay = controlledStream();
  const secondReplay = controlledStream();
  const recoveredUrls = [];
  writeHandler = () => initial.response;
  readHandler = (url) => {
    if (url.includes("/runs/run-a/events")) {
      recoveredUrls.push(url);
      return recoveredUrls.length === 1 ? firstReplay.response : secondReplay.response;
    }
  };
  await openApp();
  await send("问题 A");
  initial.emit({ type: "started", workspace_id: "w1", thread_id: "a", run_id: "run-a" }, "100-0");
  initial.emit({ type: "text_delta", message_id: "m", text: "第一段" }, "100-1");
  initial.close();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
  expect(recoveredUrls).toEqual(["/api/workspaces/w1/threads/a/runs/run-a/events?after=100-1"]);
  firstReplay.emit({ type: "text_delta", message_id: "m", text: "第二段" }, "100-2");
  firstReplay.close();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
  expect(recoveredUrls[1]).toBe("/api/workspaces/w1/threads/a/runs/run-a/events?after=100-2");
  secondReplay.emit({ type: "text_delta", message_id: "m", text: "第三段" }, "100-3");
  await flushPromises();
  expect(wrapper.text().match(/第一段第二段第三段/g)).toHaveLength(1);
  threads.a.messages = [message(1, "user", "问题 A"), message(2, "assistant", "第一段第二段第三段", "m")];
  threads.a.execution_status = "succeeded";
  secondReplay.emit({ type: "run_finished", run_id: "run-a", status: "succeeded", error: null });
  await flushPromises();
  expect(wrapper.findAll(".message.assistant")).toHaveLength(1);
  expect(wrapper.get(".message.assistant").text()).toContain("第一段第二段第三段");
  expect(wrapper.get("textarea").element.value).toBe("");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(writes).toHaveLength(1);
});

it.each(["failed", "interrupted"])("finishes replay with %s and preserves the question", async (status) => {
  vi.useFakeTimers();
  const initial = controlledStream();
  const replay = controlledStream();
  writeHandler = () => initial.response;
  readHandler = (url) => url.includes("/runs/run-a/events") ? replay.response : undefined;
  await openApp();
  await send("问题 A");
  initial.emit({ type: "started", workspace_id: "w1", thread_id: "a", run_id: "run-a" }, "100-0");
  initial.close();
  await flushPromises();
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
  threads.a.execution_status = status;
  threads.a.execution_error = "执行已结束";
  threads.a.messages = [message(1, "user", "问题 A")];
  replay.emit({ type: "run_finished", run_id: "run-a", status, error: "执行已结束" });
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("执行已结束");
  expect(wrapper.get("textarea").element.value).toBe("问题 A");
  expect(wrapper.get("textarea").element.disabled).toBe(false);
  expect(writes).toHaveLength(1);
});

it("recovers a disconnected stream through polling without resending", async () => {
  vi.useFakeTimers();
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "m", text: "部分回答" });
  threads.a.messages = [message(1, "user", "问题 A")];
  threads.a.execution_status = "running";
  stream.close();
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("连接已中断");
  expect(wrapper.get("textarea").element.disabled).toBe(true);
  threads.a.messages.push(message(2, "assistant", "完整回答", "m"));
  threads.a.execution_status = "succeeded";
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(wrapper.get(".message.assistant").text()).toContain("完整回答");
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  expect(writes).toHaveLength(1);
});

it("shows the committed answer once even if refreshing storage fails after success", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  stream.emit({ type: "text_delta", message_id: "m", text: "完整回答" });
  readHandler = () => Promise.reject(new Error("读回失败"));
  stream.emit({ type: "succeeded", message: { ...message(2, "assistant", "完整回答", "m"), thread_id: "a" } });
  await flushPromises();
  expect(wrapper.findAll(".message").map((item) => item.text())).toEqual([
    expect.stringContaining("问题 A"), expect.stringContaining("完整回答"),
  ]);
  expect(wrapper.get('[role="alert"]').text()).toContain("刷新记录失败");
  expect(wrapper.find(".research-progress").exists()).toBe(false);
  expect(wrapper.get("textarea").element.value).toBe("");
});

it("keeps polling when both the event stream and the immediate state refresh fail", async () => {
  vi.useFakeTimers();
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  threads.a.messages = [message(1, "user", "问题 A")];
  threads.a.execution_status = "running";
  readHandler = () => Promise.reject(new TypeError("offline"));
  stream.close();
  await flushPromises();
  readHandler = () => undefined;
  threads.a.messages.push(message(2, "assistant", "后台已完成"));
  threads.a.execution_status = "succeeded";
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.get(".message.assistant").text()).toContain("后台已完成");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(writes).toHaveLength(1);
});

it("closes the streaming connection on unmount without additional reads or retries", async () => {
  const stream = controlledStream();
  let signal;
  writeHandler = (url, options) => {
    signal = options.signal;
    signal.addEventListener("abort", () => stream.fail(new DOMException("Aborted", "AbortError")));
    return stream.response;
  };
  await openApp();
  await send("问题 A");
  const calls = fetch.mock.calls.length;
  wrapper.unmount();
  wrapper = null;
  await flushPromises();
  expect(signal.aborted).toBe(true);
  expect(fetch).toHaveBeenCalledTimes(calls);
});

it("stops reconciliation polling after a disconnected background thread finishes", async () => {
  vi.useFakeTimers();
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
  stream.emit({ type: "started", workspace_id: "w1", thread_id: "a" });
  threads.a.messages = [message(1, "user", "问题 A")];
  threads.a.execution_status = "running";
  await selectThread("b");
  stream.close();
  await flushPromises();
  threads.a.execution_status = "succeeded";
  threads.a.messages.push(message(2, "assistant", "后台 A 回答"));
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  const calls = fetch.mock.calls.length;
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch).toHaveBeenCalledTimes(calls);
  expect(wrapper.get(".chat-heading").text()).toContain("会话 b");
  await selectThread("a");
  expect(wrapper.get(".message.assistant").text()).toContain("后台 A 回答");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
});

it("runs two threads concurrently and keeps late answers, drafts and optimistic messages in their thread", async () => {
  vi.useFakeTimers();
  const pending = { a: deferred(), b: deferred() };
  writeHandler = (url, options) => {
    const id = url.split("/").at(-3);
    threads[id].execution_status = "running";
    threads[id].messages.push(message(1, "user", JSON.parse(options.body).content));
    return pending[id].promise;
  };
  await openApp();
  await send("问题 A");
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.findAll(".message.user")).toHaveLength(1);
  await selectThread("b");
  await send("问题 B");
  expect(writes.map((item) => item.url)).toEqual([
    "/api/workspaces/w1/threads/a/messages/stream", "/api/workspaces/w1/threads/b/messages/stream",
  ]);
  expect(wrapper.findAll(".thread-status").map((item) => item.text())).toEqual(["研究中", "研究中"]);
  threads.b.execution_status = "succeeded";
  threads.b.messages.push(message(2, "assistant", "回答 B"));
  pending.b.resolve(successResponse(threads.b.messages[1], "w1", "b"));
  await flushPromises();
  await wrapper.get('textarea[aria-label="研究问题"]').setValue("B 的下一轮草稿");
  threads.a.execution_status = "succeeded";
  threads.a.messages.push(message(2, "assistant", "回答 A"));
  pending.a.resolve(successResponse(threads.a.messages[1], "w1", "a"));
  await flushPromises();
  expect(wrapper.get(".messages").text()).toContain("回答 B");
  expect(wrapper.get(".messages").text()).not.toContain("回答 A");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.value).toBe("B 的下一轮草稿");
  await selectThread("a");
  expect(wrapper.get(".messages").text()).toContain("回答 A");
  expect(wrapper.findAll(".message")).toHaveLength(2);
});

it("keeps late failures in the originating workspace and restores them on return", async () => {
  const pending = deferred();
  writeHandler = () => pending.promise;
  await openApp();
  await send("问题 A");
  await selectWorkspace("w2");
  await wrapper.get("textarea[aria-label='研究问题']").setValue("C 的草稿");
  threads.a.messages = [message(1, "user", "问题 A")];
  threads.a.execution_status = "failed";
  threads.a.execution_error = "A 模型失败";
  pending.resolve(response({ detail: "A 模型失败" }, 502));
  await flushPromises();
  expect(wrapper.get("h1").text()).toBe("研究 w2");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(wrapper.get("textarea[aria-label='研究问题']").element.value).toBe("C 的草稿");
  await selectWorkspace("w1");
  await selectThread("a");
  expect(wrapper.get('[role="alert"]').text()).toContain("A 模型失败");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.value).toBe("问题 A");
  expect(writes).toHaveLength(1);
});

it("uploads while research is running, allows another research during upload, and scopes upload failures", async () => {
  const research = deferred();
  const upload = deferred();
  writeHandler = async (url) => {
    if (url.endsWith("/files")) return upload.promise;
    await research.promise;
    return successResponse(message(2, "assistant", "完成"),
      url.includes("/w2/") ? "w2" : "w1", url.split("/").at(-3));
  };
  await openApp();
  await send("问题 A");
  expect(wrapper.get('button[aria-label="上传资料"]').element.disabled).toBe(false);
  wrapper.getComponent(ArtifactPanel).vm.$emit("upload", new File(["evidence"], "evidence.txt"));
  await flushPromises();
  expect(wrapper.get('button[aria-label="上传资料"]').element.disabled).toBe(true);
  expect(writes[1].url).toBe("/api/workspaces/w1/files");
  expect(writes[1].body.get("file").name).toBe("evidence.txt");
  await selectWorkspace("w2");
  expect(wrapper.get('button[aria-label="上传资料"]').element.disabled).toBe(false);
  await send("问题 C");
  expect(writes[2].url).toBe("/api/workspaces/w2/threads/c/messages/stream");
  upload.resolve(response({ detail: "资料过大" }, 413));
  await flushPromises();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  await selectWorkspace("w1");
  expect(wrapper.get('.artifact-panel [role="alert"]').text()).toContain("资料过大");
  expect(wrapper.get('button[aria-label="上传资料"]').element.disabled).toBe(false);
  research.resolve(response({}, 201));
  await flushPromises();
});

it("refreshes the originating upload's files without replacing another thread's draft", async () => {
  const upload = deferred();
  writeHandler = () => upload.promise;
  await openApp();
  wrapper.getComponent(ArtifactPanel).vm.$emit("upload", new File(["data"], "evidence.txt"));
  await flushPromises();
  await selectThread("b");
  await wrapper.get("textarea[aria-label='研究问题']").setValue("B 草稿");
  spaces[0].artifacts = [{ id: "file1", name: "evidence.txt", relative_path: "workspaces/w1/uploads/evidence.txt" }];
  upload.resolve(response(spaces[0].artifacts[0], 201));
  await flushPromises();
  expect(wrapper.get(".artifact-panel").text()).toContain("evidence.txt");
  expect(wrapper.get(".chat-heading").text()).toContain("会话 b");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.value).toBe("B 草稿");
});

it.each(["succeeded", "failed", "interrupted"])("restores a backend running state after reload and polls until %s", async (status) => {
  vi.useFakeTimers();
  threads.a.execution_status = "running";
  threads.a.messages = [message(1, "user", "已有问题")];
  await openApp();
  expect(wrapper.get(".researching").text()).toContain("正在研究");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.disabled).toBe(true);
  expect(wrapper.findAll(".message")).toHaveLength(1);
  threads.a.execution_status = status;
  threads.a.execution_error = status === "succeeded" ? null : "后端终态说明";
  if (status === "succeeded") threads.a.messages.push(message(2, "assistant", "恢复的回答"));
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.find(".researching").exists()).toBe(false);
  expect(wrapper.get("textarea[aria-label='研究问题']").element.disabled).toBe(false);
  if (status === "succeeded") expect(wrapper.get(".messages").text()).toContain("恢复的回答");
  else expect(wrapper.get('[role="alert"]').text()).toContain("后端终态说明");
  const reads = fetch.mock.calls.length;
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch.mock.calls).toHaveLength(reads);
  expect(writes).toHaveLength(0);
});

it.each([409, 503])("shows a %s rejection only in its thread and never retries automatically", async (status) => {
  threads.a.execution_status = "running";
  writeHandler = () => {
    if (status === 409) threads.b.execution_status = "running";
    return response({ detail: "暂不可开始" }, status);
  };
  await openApp();
  await selectThread("b");
  await send("问题 B");
  expect(wrapper.get('[role="alert"]').text()).toContain(String(status));
  expect(wrapper.get("textarea[aria-label='研究问题']").element.value).toBe("问题 B");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.disabled).toBe(status === 409);
  await selectThread("a");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(wrapper.find(".researching").exists()).toBe(true);
  expect(writes).toHaveLength(1);
});

it("ignores an older workspace selection that finishes after a newer one", async () => {
  await openApp();
  const oldRead = deferred();
  readHandler = (url) => url === "/api/workspaces/w2" ? oldRead.promise : undefined;
  await selectWorkspace("w2");
  await selectWorkspace("w1");
  oldRead.resolve(response({ ...spaces[1], threads: [threads.c] }));
  await flushPromises();
  expect(wrapper.get("h1").text()).toBe("研究 w1");
  expect(window.location.search).toBe("?workspace=w1&thread=b");
});

it("discards a polling read when the user has switched threads", async () => {
  vi.useFakeTimers();
  threads.a.execution_status = "running";
  await openApp();
  const oldRead = deferred();
  readHandler = (url) => url.endsWith("/threads/a") ? oldRead.promise : undefined;
  await vi.advanceTimersByTimeAsync(2000);
  await selectThread("b");
  oldRead.resolve(response({ ...threads.a, messages: [message(1, "assistant", "过期回答")] }));
  await flushPromises();
  expect(wrapper.get(".chat-heading").text()).toContain("会话 b");
  expect(wrapper.get(".messages").text()).not.toContain("过期回答");
});

it("can create and start a new thread while another is running", async () => {
  const pending = deferred();
  writeHandler = (url, options) => {
    if (url.endsWith("/threads")) {
      threads.d = { ...threads.b, id: "d", title: JSON.parse(options.body).title };
      return response(threads.d, 201);
    }
    return pending.promise.then(() => successResponse(message(2, "assistant", "完成"), "w1", url.split("/").at(-3)));
  };
  await openApp();
  await send("问题 A");
  await wrapper.get('button[aria-label="新建会话"]').trigger("click");
  await wrapper.get('input[name="title"]').setValue("新会话 D");
  await wrapper.get("dialog form").trigger("submit");
  await flushPromises();
  expect(wrapper.get(".chat-heading").text()).toContain("新会话 D");
  await send("问题 D");
  expect(writes.map((item) => item.url)).toEqual([
    "/api/workspaces/w1/threads/a/messages/stream", "/api/workspaces/w1/threads",
    "/api/workspaces/w1/threads/d/messages/stream",
  ]);
  pending.resolve(response({}, 201));
  await flushPromises();
});

it("reports a polling failure, recovers on the next read, and stops polling on unmount", async () => {
  vi.useFakeTimers();
  threads.a.execution_status = "running";
  await openApp();
  readHandler = () => Promise.reject(new TypeError("offline"));
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("自动刷新失败");
  expect(wrapper.get("textarea[aria-label='研究问题']").element.disabled).toBe(true);
  readHandler = () => undefined;
  await vi.advanceTimersByTimeAsync(2000);
  await flushPromises();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  wrapper.unmount();
  wrapper = null;
  const reads = fetch.mock.calls.length;
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch.mock.calls).toHaveLength(reads);
  expect(writes).toHaveLength(0);
});

it("keeps a later navigation loading when an earlier manual refresh finishes", async () => {
  await openApp();
  const refresh = deferred();
  const navigation = deferred();
  readHandler = (url) => {
    if (url === "/api/workspaces/w1") return refresh.promise;
    if (url === "/api/workspaces/w2") return navigation.promise;
  };
  await wrapper.get('button[aria-label="刷新研究"]').trigger("click");
  await selectWorkspace("w2");
  refresh.resolve(response({ ...spaces[0], threads: [threads.a, threads.b] }));
  await flushPromises();
  expect(wrapper.get('[role="status"]').text()).toContain("正在读取研究空间");
  expect(wrapper.get(".new-workspace").element.disabled).toBe(true);
  navigation.resolve(response({ ...spaces[1], threads: [threads.c] }));
  await flushPromises();
  expect(wrapper.get("h1").text()).toBe("研究 w2");
});
