<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref, watch } from "vue";
import {
  ArrowUp,
  Plus,
  MessageSquare,
  RefreshCw,
  ArrowUpRight,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  X,
  BookOpen,
  CircleAlert,
  Layers,
  ChevronRight,
} from "@lucide/vue";
import { jsonRequest, request, streamRequest } from "./api";
import MarkdownContent from "./components/MarkdownContent.vue";
import ArtifactPanel from "./components/ArtifactPanel.vue";

const workspaces = ref([]);
const workspace = ref(null);
const thread = ref(null);
const loading = ref(true);
const runs = reactive({});
const drafts = reactive({});
const uploads = reactive({});
const saving = ref(false);
const error = ref("");
const search = ref("");
const threadSearch = ref("");
const pollError = ref("");
const connections = new Set();
let disposed = false;
let selectionVersion = 0;
let refreshVersion = 0;
const draft = computed({
  get: () => drafts[thread.value?.id] || "",
  set: (value) => {
    drafts[thread.value.id] = value;
  },
});
const sending = computed(
  () => Boolean(runs[thread.value?.id]?.pending) ||
    thread.value?.execution_status === "running",
);
const currentRun = computed(() => runs[thread.value?.id]);
const displayedMessages = computed(() => {
  const messages = thread.value?.messages || [];
  const run = currentRun.value;
  if (!run?.answer || messages.some((item) => item.id === run.answer.id)) return messages;
  const question = messages.filter((item) => item.role === "user").length === run.userMessageCount
    ? [{ id: `pending:${thread.value.id}`, role: "user", content: run.content, created_at: run.createdAt }] : [];
  return [...messages, ...question, run.answer];
});
const pendingText = computed(() => {
  const run = runs[thread.value?.id];
  // A refresh may already contain the persisted user message.
  return run?.pending && !run.answer &&
    thread.value.messages.filter((item) => item.role === "user").length === run.userMessageCount
    ? run.content
    : "";
});
const conversationEntries = computed(() => {
  const entries = displayedMessages.value.map((message) => ({
    type: "message", key: `message:${message.id}`, message,
  }));
  if (pendingText.value) entries.push({ type: "pending", key: "pending" });
  const run = currentRun.value;
  if (!run) return entries;

  // Preserve event order around saved messages, including a final answer received
  // before the preceding plan has been read back from storage.
  const textIds = new Set(run.items.filter((item) => item.type === "text").map((item) => item.id));
  const firstSaved = entries.findIndex((entry) => entry.type === "message" &&
    textIds.has(entry.message.model_message_id));
  let insertAt = firstSaved === -1 ? entries.length : firstSaved;
  let progress = null;
  for (const item of run.items) {
    if (item.type === "text") {
      const saved = entries.findIndex((entry) => entry.type === "message" &&
        entry.message.model_message_id === item.id);
      if (saved !== -1) {
        insertAt = saved + 1;
        progress = null;
        continue;
      }
    }
    if (!progress) {
      progress = { type: "progress", key: `progress:${item.type}:${item.id}`, items: [] };
      entries.splice(insertAt++, 0, progress);
    }
    progress.items.push(item);
  }
  return entries;
});
const executionError = computed(() => {
  const run = runs[thread.value?.id];
  if (run?.error) return run.error;
  if (run?.pending) return "";
  if (thread.value?.execution_status === "failed")
    return `上次研究失败：${thread.value.execution_error}`;
  if (thread.value?.execution_status === "interrupted")
    return `上次研究已中断：${thread.value.execution_error}`;
  return "";
});
const sidebarOpen = ref(false);
const messageEnd = ref(null);
const modal = ref(null);
const modalKind = ref("");
const formError = ref("");
const form = reactive({
  title: "",
  goal: "",
  initial_hypothesis: "",
  scope: "",
  observation_period: "",
  preferences: "{}",
});
const busy = computed(() => loading.value || saving.value);
const filteredWorkspaces = computed(() =>
  workspaces.value.filter((item) =>
    `${item.title} ${item.goal}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
);
const filteredThreads = computed(() =>
  workspace.value
    ? workspace.value.threads.filter((item) =>
        item.title.toLowerCase().includes(threadSearch.value.toLowerCase()),
      )
    : [],
);
const workspacePath = computed(() => `/workspaces/${workspace.value.id}`);
const dialogTitle = computed(
  () =>
    ({
      workspace: "创建研究空间",
      thread: "开启新会话",
      details: "研究档案",
    })[modalKind.value],
);

function rememberSelection() {
  const url = new URL(window.location.href);
  url.searchParams.set("workspace", workspace.value.id);
  if (thread.value) url.searchParams.set("thread", thread.value.id);
  else url.searchParams.delete("thread");
  window.history.replaceState(null, "", url);
}

function reconcileRun(detail) {
  const run = runs[detail.id];
  if (!run || (!run.reconciling && run.status !== "disconnected")) return;
  run.reconciling = false;
  if (run.status === "disconnected" &&
      detail.messages.filter((item) => item.role === "user").length > run.userMessageCount &&
      ["succeeded", "failed", "interrupted"].includes(detail.execution_status)) {
    run.status = detail.execution_status;
    run.error = detail.execution_status === "succeeded" ? "" : detail.execution_error;
  }
}

async function loadSelection(id, threadId) {
  const version = ++selectionVersion;
  loading.value = true;
  pollError.value = "";
  threadSearch.value = "";
  workspace.value = null;
  thread.value = null;
  try {
    const detail = await request(`/workspaces/${id}`);
    if (version !== selectionVersion) return;
    const selected =
      detail.threads.find((item) => item.id === threadId) || detail.threads.at(-1);
    const selectedThread = selected
      ? await request(`/workspaces/${id}/threads/${selected.id}`)
      : null;
    if (version !== selectionVersion) return;
    workspace.value = detail;
    thread.value = selectedThread;
    if (selectedThread) reconcileRun(selectedThread);
    rememberSelection();
  } catch (err) {
    if (version === selectionVersion) throw err;
  } finally {
    if (version === selectionVersion) loading.value = false;
  }
}

async function initialize() {
  let version = ++selectionVersion;
  loading.value = true;
  error.value = "";
  try {
    const list = await request("/workspaces");
    if (version !== selectionVersion) return;
    workspaces.value = list;
    const params = new URLSearchParams(window.location.search);
    const selected =
      workspaces.value.find((item) => item.id === params.get("workspace")) ||
      workspaces.value[0];
    if (selected) {
      const selection = loadSelection(selected.id, params.get("thread"));
      version = selectionVersion;
      await selection;
    }
  } catch (err) {
    if (version === selectionVersion) error.value = err.message;
  } finally {
    if (version === selectionVersion) loading.value = false;
  }
}

async function selectWorkspace(id, threadId) {
  error.value = "";
  sidebarOpen.value = false;
  try {
    await loadSelection(id, threadId);
  } catch (err) {
    error.value = err.message;
  }
}

async function selectThread(id) {
  const version = ++selectionVersion;
  const workspaceId = workspace.value.id;
  loading.value = true;
  error.value = "";
  sidebarOpen.value = false;
  thread.value = null;
  try {
    const selected = await request(`/workspaces/${workspaceId}/threads/${id}`);
    if (version !== selectionVersion) return;
    thread.value = selected;
    reconcileRun(selected);
    rememberSelection();
  } catch (err) {
    if (version === selectionVersion) error.value = err.message;
  } finally {
    if (version === selectionVersion) loading.value = false;
  }
}

async function refreshResearch(workspaceId = workspace.value.id) {
  // Background work only refreshes its own workspace; navigation reloads other workspaces.
  if (workspace.value?.id !== workspaceId) return;
  const selection = selectionVersion;
  const version = ++refreshVersion;
  const threadId = thread.value?.id;
  const path = `/workspaces/${workspaceId}`;
  const reads = [request(path), request("/workspaces")];
  if (threadId) reads.push(request(`${path}/threads/${threadId}`));
  const results = await Promise.allSettled(reads);
  if (selection !== selectionVersion || version !== refreshVersion) return;
  if (results[0].status === "fulfilled") {
    workspace.value = results[0].value;
    // Non-selected threads are observed through the workspace status list.
    for (const item of workspace.value.threads) {
      if (item.id !== threadId && runs[item.id]) runs[item.id].reconciling = false;
    }
  }
  if (results[1].status === "fulfilled") workspaces.value = results[1].value;
  if (threadId && results[2].status === "fulfilled") {
    thread.value = results[2].value;
    reconcileRun(thread.value);
  }
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length)
    throw new Error(failures.map((result) => result.reason.message).join("；"));
}

async function refresh() {
  const version = selectionVersion;
  loading.value = true;
  error.value = "";
  try {
    await refreshResearch();
  } catch (err) {
    if (version === selectionVersion) error.value = err.message;
  } finally {
    if (version === selectionVersion) loading.value = false;
  }
}

async function sendMessage() {
  if (busy.value || sending.value || !draft.value.trim() || !thread.value) return;
  const workspaceId = workspace.value.id;
  const threadId = thread.value.id;
  runs[threadId] = {
    pending: true,
    content: draft.value.trim(),
    createdAt: new Date().toISOString(),
    userMessageCount: thread.value.messages.filter((item) => item.role === "user")
      .length,
    error: "",
    status: "connecting",
    items: [],
    answer: null,
    reconciling: false,
  };
  const run = runs[threadId];
  const controller = new AbortController();
  connections.add(controller);
  try {
    const terminal = await streamRequest(
      `/workspaces/${workspaceId}/threads/${threadId}/messages/stream`,
      { content: run.content },
      (event) => {
        switch (event.type) {
          case "started":
            if (event.workspace_id !== workspaceId || event.thread_id !== threadId)
              throw new Error("研究事件归属不匹配。");
            run.status = "running";
            break;
          case "text_delta": {
            let item = run.items.find((item) => item.type === "text" && item.id === event.message_id);
            if (!item) {
              run.items.push({ type: "text", id: event.message_id, text: "" });
              item = run.items.at(-1);
            }
            item.text += event.text;
            break;
          }
          case "tool_start":
            run.items.push({ type: "tool", id: event.tool_run_id, name: event.name, status: "running" });
            break;
          case "tool_end":
          case "tool_error": {
            const item = run.items.find((item) => item.type === "tool" && item.id === event.tool_run_id);
            item.status = event.type === "tool_end" ? "returned" : "error";
            item.error = event.error;
            break;
          }
          case "succeeded":
            run.status = "succeeded";
            run.answer = event.message;
            break;
          case "failed":
            run.status = "failed";
            run.error = `研究失败：${event.error || "后端执行失败"} 请核对消息记录后再决定是否重发。`;
            break;
          default:
            throw new Error(`无法识别研究事件：${event.type}`);
        }
      },
      controller.signal,
    );
    if (terminal.type === "succeeded") drafts[threadId] = "";
  } catch (err) {
    if (disposed) return;
    run.status = err.status ? "rejected" : "disconnected";
    run.reconciling = true;
    const detail = err.status ? err.message : `事件连接中断，研究可能仍在后台进行。${err.message}`;
    run.error = `${detail} 请先核对消息记录；重新发送会发起新的请求。`;
  } finally {
    connections.delete(controller);
  }
  if (disposed) return;
  try {
    await refreshResearch(workspaceId);
  } catch (err) {
    run.error += `${run.error ? " " : ""}刷新记录失败：${err.message}`;
  } finally {
    run.pending = false;
  }
}

function executionLabel(item) {
  const status = runs[item.id]?.pending
    ? "running"
    : thread.value?.id === item.id
      ? thread.value.execution_status
      : item.execution_status;
  return {
    running: "研究中", succeeded: "已完成", failed: "失败", interrupted: "已中断",
  }[status];
}

watch(
  () => workspace.value && (
    sending.value || workspace.value.threads.some((item) =>
      item.execution_status === "running" || runs[item.id]?.pending || runs[item.id]?.reconciling,
    )
  ) ? workspace.value.id : null,
  (workspaceId, _, onCleanup) => {
    pollError.value = "";
    if (!workspaceId) return;
    let active = true;
    let timer;
    onCleanup(() => {
      active = false;
      clearTimeout(timer);
    });
    async function poll() {
      try {
        await refreshResearch(workspaceId);
        if (active) pollError.value = "";
      } catch (err) {
        if (active) pollError.value = `自动刷新失败：${err.message}`;
      } finally {
        if (active) timer = setTimeout(poll, 2000);
      }
    }
    timer = setTimeout(poll, 2000);
  },
);

async function openModal(kind) {
  sidebarOpen.value = false;
  modalKind.value = kind;
  formError.value = "";
  Object.assign(form, {
    title: "",
    goal: "",
    initial_hypothesis: "",
    scope: "",
    observation_period: "",
    preferences: "{}",
  });
  if (kind === "details") {
    Object.assign(form, {
      goal: workspace.value.info.goal,
      initial_hypothesis: workspace.value.info.initial_hypothesis,
      scope: workspace.value.info.scope,
      observation_period: workspace.value.info.observation_period,
      preferences: JSON.stringify(workspace.value.info.preferences, null, 2),
    });
  }
  modal.value.showModal();
  await nextTick();
  modal.value.querySelector("input, textarea, button")?.focus();
}

function closeModal() {
  if (!saving.value) modal.value.close();
}

async function saveModal() {
  formError.value = "";
  let preferences;
  if (modalKind.value === "workspace" || modalKind.value === "details") {
    try {
      preferences = JSON.parse(form.preferences);
      if (
        !preferences ||
        Array.isArray(preferences) ||
        typeof preferences !== "object"
      )
        throw new Error('请输入 JSON 对象，例如 {"报告语言": "中文"}');
    } catch (err) {
      formError.value = `偏好格式不正确：${err.message}`;
      return;
    }
  }
  saving.value = true;
  let completed = false;
  try {
    if (modalKind.value === "workspace") {
      const created = await jsonRequest("/workspaces", {
        title: form.title.trim(),
        goal: form.goal.trim(),
        initial_hypothesis: form.initial_hypothesis.trim(),
        scope: form.scope.trim(),
        observation_period: form.observation_period.trim(),
        preferences,
      });
      // Close a successful form before refresh so a failed read cannot cause a duplicate create.
      completed = true;
      modal.value.close();
      await loadSelection(created.id);
      workspaces.value = await request("/workspaces");
    } else if (modalKind.value === "thread") {
      const created = await jsonRequest(`${workspacePath.value}/threads`, {
        title: form.title.trim(),
      });
      completed = true;
      modal.value.close();
      ++selectionVersion;
      thread.value = { ...created, messages: [] };
      threadSearch.value = "";
      draft.value = "";
      rememberSelection();
      await refreshResearch();
    } else {
      await jsonRequest(
        `${workspacePath.value}/research`,
        {
          goal: form.goal.trim(),
          initial_hypothesis: form.initial_hypothesis.trim(),
          scope: form.scope.trim(),
          observation_period: form.observation_period.trim(),
          preferences,
        },
        "PUT",
      );
      completed = true;
      modal.value.close();
      await refreshResearch();
    }
    error.value = "";
  } catch (err) {
    if (completed) error.value = `已保存，但刷新失败：${err.message}`;
    else formError.value = err.message;
  } finally {
    saving.value = false;
  }
}

async function uploadFile(file) {
  const workspaceId = workspace.value.id;
  if (uploads[workspaceId]?.pending) return;
  uploads[workspaceId] = { pending: true, error: "" };
  const upload = uploads[workspaceId];
  let uploaded = false;
  try {
    const body = new FormData();
    body.append("file", file);
    await request(`/workspaces/${workspaceId}/files`, { method: "POST", body });
    uploaded = true;
    await refreshResearch(workspaceId);
  } catch (err) {
    upload.error = `${uploaded ? "文件已上传，但刷新失败：" : "上传失败："}${err.message}`;
  } finally {
    upload.pending = false;
  }
}

function composerKeydown(event) {
  if (
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey) &&
    !event.isComposing
  ) {
    event.preventDefault();
    sendMessage();
  }
}

watch(
  () => [thread.value?.id, displayedMessages.value.length, sending.value,
    currentRun.value?.items.map((item) => item.text || item.status).join("")],
  async () => {
    await nextTick();
    messageEnd.value?.scrollIntoView({ behavior: "smooth", block: "end" });
  },
);
onMounted(initialize);
onBeforeUnmount(() => {
  disposed = true;
  for (const controller of connections) controller.abort();
});
</script>

<template>
  <div class="workbench">
    <button
      v-if="sidebarOpen"
      class="sidebar-backdrop"
      aria-label="关闭导航"
      @click="sidebarOpen = false"
    ></button>
    <aside
      class="sidebar"
      :class="{ 'is-open': sidebarOpen }"
      aria-label="研究导航"
    >
      <div class="brand">
        <span class="brand-mark">C<span></span></span
        ><span>CorpCom<small>持续研究工作台</small></span
        ><button
          class="icon-button mobile-only"
          aria-label="收起导航"
          @click="sidebarOpen = false"
        >
          <PanelLeftClose :size="18" />
        </button>
      </div>
      <button
        class="new-workspace"
        :disabled="busy"
        @click="openModal('workspace')"
      >
        <Plus :size="18" />创建研究空间
      </button>
      <label class="search-box"
        ><Search :size="15" /><input
          v-model="search"
          aria-label="搜索研究空间"
          placeholder="搜索研究空间"
      /></label>
      <div class="sidebar-section-label">
        研究空间 <span>{{ workspaces.length }}</span>
      </div>
      <nav class="workspace-list" aria-label="研究空间列表">
        <button
          v-for="item in filteredWorkspaces"
          :key="item.id"
          :disabled="saving"
          class="workspace-item"
          :class="{ active: workspace?.id === item.id }"
          :aria-current="workspace?.id === item.id ? 'page' : undefined"
          @click="selectWorkspace(item.id)"
        >
          <Layers :size="17" /><span>{{ item.title }}</span
          ><ChevronRight v-if="workspace?.id === item.id" :size="14" />
        </button>
        <p v-if="!loading && !filteredWorkspaces.length" class="sidebar-empty">
          {{ search ? "没有匹配的研究" : "还没有研究空间" }}
        </p>
      </nav>
      <template v-if="workspace">
        <label class="search-box thread-search">
          <Search :size="15" /><input
            v-model="threadSearch"
            aria-label="搜索会话"
            placeholder="搜索会话"
          />
        </label>
        <div class="sidebar-section-label">
          当前研究的会话<button
            class="icon-button"
            aria-label="新建会话"
            :disabled="busy"
            @click="openModal('thread')"
          >
            <Plus :size="16" />
          </button>
        </div>
        <nav class="thread-list" aria-label="会话列表">
          <button
            v-for="item in filteredThreads"
            :key="item.id"
            :disabled="busy"
            class="thread-item"
            :class="{ active: thread?.id === item.id }"
            @click="selectThread(item.id)"
          >
            <MessageSquare :size="15" /><span>{{ item.title }}</span>
            <small v-if="executionLabel(item)" class="thread-status">{{
              executionLabel(item)
            }}</small>
          </button>
          <p v-if="!filteredThreads.length" class="sidebar-empty">
            {{ threadSearch ? "没有匹配的会话" : "新建会话，开始第一次研究。" }}
          </p>
        </nav>
      </template>
    </aside>

    <main class="main-panel">
      <header class="topbar">
        <div class="breadcrumb">
          <button
            class="icon-button mobile-only"
            aria-label="展开导航"
            @click="sidebarOpen = true"
          >
            <PanelLeftOpen :size="19" /></button
          ><span>研究工作台</span><ChevronRight :size="14" /><strong>{{
            workspace?.title || "开始研究"
          }}</strong>
        </div>
        <button
          v-if="workspace"
          class="icon-button"
          :disabled="busy"
          aria-label="刷新研究"
          title="刷新研究"
          @click="refresh"
        >
          <RefreshCw :size="17" :class="{ spin: loading }" />
        </button>
        <span v-else class="local-label">LOCAL WORKSPACE</span>
      </header>
      <div v-if="error" class="error-banner" role="alert">
        <CircleAlert :size="18" /><span>{{ error }}</span
        ><button
          class="text-button"
          :disabled="busy"
          @click="workspace ? refresh() : initialize()"
        >
          重新加载</button
        ><button
          class="icon-button"
          aria-label="关闭错误提示"
          @click="error = ''"
        >
          <X :size="16" />
        </button>
      </div>
      <div v-if="loading && !workspace" class="welcome">
        <RefreshCw class="spin" :size="28" />
        <p role="status">正在读取研究空间…</p>
      </div>
      <template v-else-if="workspace">
        <section class="research-header">
          <h1>{{ workspace.title }}</h1>
          <div class="research-meta">
            <button
              class="meta-button"
              :disabled="busy"
              @click="openModal('details')"
            >
              <BookOpen :size="14" />研究档案<ArrowUpRight :size="13" />
            </button>
          </div>
        </section>
        <div class="chat-heading">
          <div>
            <MessageSquare :size="16" />
            <h2>{{ thread?.title || "研究对话" }}</h2>
          </div>
          <span v-if="thread">{{ thread.messages.length }} 条消息</span>
        </div>
        <div v-if="executionError" class="error-banner" role="alert">
          <CircleAlert :size="18" /><span>{{ executionError }}</span>
        </div>
        <div v-if="pollError" class="error-banner" role="alert">{{ pollError }}</div>
        <div
          class="messages"
          aria-label="对话记录"
          :aria-busy="sending || loading"
        >
          <div v-if="loading" class="status-line muted" role="status">
            <RefreshCw class="spin" :size="16" />正在读取…
          </div>
          <div v-else-if="!thread" class="chat-empty">
            <h2>为这项研究开启第一次对话</h2>
            <p>新建会话后，即可发送研究任务。</p>
            <button
              class="primary-button"
              :disabled="busy"
              @click="openModal('thread')"
            >
              <Plus :size="16" />新建会话
            </button>
          </div>
          <template v-for="entry in conversationEntries" :key="entry.key">
            <article
              v-if="entry.type === 'message'"
              class="message"
              :class="entry.message.role"
            >
              <div class="message-byline">
                <span class="avatar">{{
                  entry.message.role === "assistant" ? "C" : "你"
                }}</span
                ><strong>{{
                  entry.message.role === "assistant" ? "CorpCom" : "你"
                }}</strong
                ><time>{{
                  new Date(entry.message.created_at).toLocaleTimeString("zh-CN", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })
                }}</time>
              </div>
              <MarkdownContent
                v-if="entry.message.role === 'assistant'"
                :content="entry.message.content"
              />
              <div v-else class="user-content">{{ entry.message.content }}</div>
            </article>
            <article v-else-if="entry.type === 'pending'" class="message user">
              <div class="message-byline">
                <span class="avatar">你</span><strong>你</strong
                ><span class="muted">发送中</span>
              </div>
              <div class="user-content">{{ pendingText }}</div>
            </article>
            <details v-else class="research-progress"
              :open="currentRun.status !== 'succeeded'">
              <summary>{{ currentRun.status === 'succeeded' ? '查看本轮研究过程' :
                currentRun.status === 'failed' ? '研究过程（未完成）' :
                currentRun.status === 'disconnected' ? '已收到的研究过程（连接中断）' : '研究过程' }}</summary>
              <div v-for="item in entry.items" :key="`${item.type}:${item.id}`" class="progress-item">
                <MarkdownContent v-if="item.type === 'text'" :content="item.text" />
                <div v-else class="tool-event" :class="item.status">
                  <span>{{ item.name }}</span>
                  <span>{{ item.status === 'running' ?
                    (currentRun.pending ? '执行中' : '结果未收到') :
                    item.status === 'returned' ? '已返回' : '执行异常' }}</span>
                  <p v-if="item.error">{{ item.error }}</p>
                </div>
              </div>
            </details>
          </template>
          <div v-if="sending" class="researching" role="status">
            <span class="pulse-dot"></span>
            <div>
              <strong>正在研究，请稍候…</strong>
              <p>可以切换会话、开展其他研究或上传资料；返回后会更新进度。</p>
            </div>
          </div>
          <div ref="messageEnd"></div>
        </div>
        <form class="composer" @submit.prevent="sendMessage">
          <div class="composer-box">
            <textarea
              v-model="draft"
              aria-label="研究问题"
              :disabled="busy || sending || !thread"
              :placeholder="
                thread
                  ? '提出新问题，或继续已有研究…'
                  : '新建会话后，即可发送研究任务'
              "
              rows="3"
              @keydown="composerKeydown"
            ></textarea>
            <div class="composer-bottom">
              <span>{{
                sending ? "研究进行中" : "⌘ / Ctrl + Enter 发送，Enter 换行"
              }}</span
              ><button
                class="send-button"
                type="submit"
                aria-label="发送研究任务"
                :disabled="busy || sending || !thread || !draft.trim()"
              >
                <RefreshCw v-if="sending" class="spin" :size="18" /><ArrowUp
                  v-else
                  :size="20"
                />
              </button>
            </div>
          </div>
          <p class="composer-note">
            数据来源于网络，生成内容不构成投资建议或买卖任何证券的推荐。请自行核实信息并做好尽职调查。
          </p>
        </form>
      </template>
      <div v-else class="welcome">
        <span class="eyebrow">WELCOME TO CORPCOM</span>
        <h1>让每一次研究，<br />都有迹可循。</h1>
        <p>
          围绕一个目标，积累证据、验证假设，<br />在新的会话里继续推进研究。
        </p>
        <button
          class="primary-button"
          :disabled="busy"
          @click="openModal('workspace')"
        >
          <Plus :size="17" />创建第一个研究空间
        </button>
        <div class="welcome-steps">
          <span>01 明确目标</span><span>02 收集证据</span
          ><span>03 持续验证</span>
        </div>
      </div>
    </main>
    <ArtifactPanel
      v-if="workspace"
      :key="workspace.id"
      :workspace="workspace"
      :busy="Boolean(uploads[workspace.id]?.pending)"
      :upload-error="uploads[workspace.id]?.error || ''"
      @upload="uploadFile"
    />

    <dialog
      ref="modal"
      class="modal"
      aria-labelledby="dialog-title"
      @cancel="saving && $event.preventDefault()"
      @click="$event.target === modal && closeModal()"
    >
      <div class="modal-header">
        <div>
          <span class="eyebrow">CORPCOM WORKSPACE</span>
          <h2 id="dialog-title">{{ dialogTitle }}</h2>
        </div>
        <button
          class="icon-button"
          aria-label="关闭弹窗"
          :disabled="saving"
          @click="closeModal"
        >
          <X :size="20" />
        </button>
      </div>
      <form @submit.prevent="saveModal">
        <fieldset :disabled="saving">
          <template v-if="modalKind === 'workspace' || modalKind === 'thread'"
            ><label
              >标题 <span class="required">*</span
              ><input
                v-model="form.title"
                name="title"
                required
                :placeholder="
                  modalKind === 'workspace'
                    ? '例如：白酒企业经营韧性研究'
                    : '例如：2025 年报增量研究'
                " /></label
          ></template>
          <template v-if="modalKind === 'workspace' || modalKind === 'details'"
            ><label
              >研究目标（选填）<textarea
                v-model="form.goal"
                name="goal"
                rows="2"
                placeholder="这项研究要回答什么问题？"
              ></textarea></label
            ><label
              >初始假设（选填）<textarea
                v-model="form.initial_hypothesis"
                name="initial_hypothesis"
                rows="2"
                placeholder="写下待验证的判断，不预设结论"
              ></textarea>
            </label>
            <div class="form-row">
              <label
                >研究范围（选填）<input
                  v-model="form.scope"
                  name="scope"
                  placeholder="公司、行业或事件" /></label
              ><label
                >观察周期（选填）<input
                  v-model="form.observation_period"
                  name="observation_period"
                  placeholder="例如：2023—2025 年度"
              /></label></div
          ></template>
          <label v-if="modalKind === 'workspace' || modalKind === 'details'"
            >偏好（JSON 对象）<textarea
              v-model="form.preferences"
              name="preferences"
              class="code-input"
              :rows="modalKind === 'details' ? 7 : 2"
              spellcheck="false"
            ></textarea
            ><small
              >例如：{"报告语言": "中文", "关注重点":
              "现金流"}。保存后整体替换现有偏好。</small
            ></label
          >
        </fieldset>
        <p v-if="formError" class="error-message" role="alert">
          {{ formError }}
        </p>
        <div class="modal-footer">
          <button
            class="secondary-button"
            type="button"
            :disabled="saving"
            @click="closeModal"
          >
            取消</button
          ><button class="primary-button" type="submit" :disabled="saving">
            <RefreshCw v-if="saving" class="spin" :size="16" />{{
              saving ? "保存中…" : modalKind === "details" ? "保存研究信息" : "创建"
            }}
          </button>
        </div>
      </form>
    </dialog>
  </div>
</template>
