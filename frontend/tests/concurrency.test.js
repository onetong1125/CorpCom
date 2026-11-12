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

it("streams text and tool events into the original thread and replaces the final increment once", async () => {
  const stream = controlledStream();
  writeHandler = () => stream.response;
  await openApp();
  await send("问题 A");
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
