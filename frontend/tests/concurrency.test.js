import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import App from "../src/App.vue";
import ArtifactPanel from "../src/components/ArtifactPanel.vue";

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

function message(id, role, content) {
  return { id, role, content, created_at: "2026-11-10T00:00:00Z" };
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

it("runs two threads concurrently and keeps late answers, drafts and optimistic messages in their thread", async () => {
  vi.useFakeTimers();
  const pending = { a: deferred(), b: deferred() };
  writeHandler = (url, options) => {
    const id = url.split("/").at(-2);
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
    "/api/workspaces/w1/threads/a/messages", "/api/workspaces/w1/threads/b/messages",
  ]);
  expect(wrapper.findAll(".thread-status").map((item) => item.text())).toEqual(["研究中", "研究中"]);
  threads.b.execution_status = "succeeded";
  threads.b.messages.push(message(2, "assistant", "回答 B"));
  pending.b.resolve(response(threads.b.messages[1], 201));
  await flushPromises();
  await wrapper.get('textarea[aria-label="研究问题"]').setValue("B 的下一轮草稿");
  threads.a.execution_status = "succeeded";
  threads.a.messages.push(message(2, "assistant", "回答 A"));
  pending.a.resolve(response(threads.a.messages[1], 201));
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
  writeHandler = (url) => url.endsWith("/files") ? upload.promise : research.promise;
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
  expect(writes[2].url).toBe("/api/workspaces/w2/threads/c/messages");
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
    return pending.promise;
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
    "/api/workspaces/w1/threads/a/messages", "/api/workspaces/w1/threads",
    "/api/workspaces/w1/threads/d/messages",
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
