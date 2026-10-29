<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
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
import { jsonRequest, request } from "./api";
import MarkdownContent from "./components/MarkdownContent.vue";
import ArtifactPanel from "./components/ArtifactPanel.vue";

const workspaces = ref([]);
const workspace = ref(null);
const thread = ref(null);
const loading = ref(true);
const sending = ref(false);
const saving = ref(false);
const error = ref("");
const search = ref("");
const threadSearch = ref("");
const draft = ref("");
const pendingText = ref("");
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
const busy = computed(() => loading.value || saving.value || sending.value);
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

async function loadSelection(id, threadId) {
  threadSearch.value = "";
  workspace.value = null;
  thread.value = null;
  draft.value = "";
  const detail = await request(`/workspaces/${id}`);
  workspace.value = detail;
  const selected =
    detail.threads.find((item) => item.id === threadId) ||
    detail.threads.at(-1);
  if (selected)
    thread.value = await request(`/workspaces/${id}/threads/${selected.id}`);
  rememberSelection();
}

async function initialize() {
  loading.value = true;
  error.value = "";
  try {
    workspaces.value = await request("/workspaces");
    const params = new URLSearchParams(window.location.search);
    const selected =
      workspaces.value.find((item) => item.id === params.get("workspace")) ||
      workspaces.value[0];
    if (selected) await loadSelection(selected.id, params.get("thread"));
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function selectWorkspace(id) {
  loading.value = true;
  error.value = "";
  sidebarOpen.value = false;
  try {
    await loadSelection(id);
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function selectThread(id) {
  loading.value = true;
  error.value = "";
  sidebarOpen.value = false;
  draft.value = "";
  thread.value = null;
  try {
    thread.value = await request(`${workspacePath.value}/threads/${id}`);
    rememberSelection();
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function refreshResearch() {
  // Reload both indexes and persisted messages, including after a failed model request.
  const reads = [request(workspacePath.value), request("/workspaces")];
  if (thread.value)
    reads.push(request(`${workspacePath.value}/threads/${thread.value.id}`));
  const results = await Promise.allSettled(reads);
  if (results[0].status === "fulfilled") workspace.value = results[0].value;
  if (results[1].status === "fulfilled") workspaces.value = results[1].value;
  if (thread.value && results[2].status === "fulfilled")
    thread.value = results[2].value;
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length)
    throw new Error(failures.map((result) => result.reason.message).join("；"));
}

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    await refreshResearch();
  } catch (err) {
    error.value = err.message;
  } finally {
    loading.value = false;
  }
}

async function sendMessage() {
  if (busy.value || !draft.value.trim() || !thread.value) return;
  sending.value = true;
  error.value = "";
  pendingText.value = draft.value.trim();
  try {
    await jsonRequest(
      `${workspacePath.value}/threads/${thread.value.id}/messages`,
      { content: pendingText.value },
    );
    draft.value = "";
  } catch (err) {
    error.value = `${err.message}。请先核对消息记录；重新发送会发起新的请求。`;
  }
  try {
    await refreshResearch();
  } catch (err) {
    error.value += `${error.value ? " " : ""}刷新记录失败：${err.message}`;
  } finally {
    sending.value = false;
    pendingText.value = "";
  }
}

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
  saving.value = true;
  error.value = "";
  let uploaded = false;
  try {
    const body = new FormData();
    body.append("file", file);
    await request(`${workspacePath.value}/files`, { method: "POST", body });
    uploaded = true;
    await refreshResearch();
  } catch (err) {
    error.value = `${uploaded ? "文件已上传，但刷新失败：" : "上传失败："}${err.message}`;
  } finally {
    saving.value = false;
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
  () => [thread.value?.id, thread.value?.messages.length, sending.value],
  async () => {
    await nextTick();
    messageEnd.value?.scrollIntoView({ behavior: "smooth", block: "end" });
  },
);
onMounted(initialize);
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
          :disabled="busy"
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
          <article
            v-for="message in thread?.messages || []"
            :key="message.id"
            class="message"
            :class="message.role"
          >
            <div class="message-byline">
              <span class="avatar">{{
                message.role === "assistant" ? "C" : "你"
              }}</span
              ><strong>{{
                message.role === "assistant" ? "CorpCom" : "你"
              }}</strong
              ><time>{{
                new Date(message.created_at).toLocaleTimeString("zh-CN", {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              }}</time>
            </div>
            <MarkdownContent
              v-if="message.role === 'assistant'"
              :content="message.content"
            />
            <div v-else class="user-content">{{ message.content }}</div>
          </article>
          <article v-if="sending" class="message user">
            <div class="message-byline">
              <span class="avatar">你</span><strong>你</strong
              ><span class="muted">发送中</span>
            </div>
            <div class="user-content">{{ pendingText }}</div>
          </article>
          <div v-if="sending" class="researching" role="status">
            <span class="pulse-dot"></span>
            <div>
              <strong>正在研究，请稍候…</strong>
              <p>完成后会显示回答并刷新研究产物。请保持页面打开。</p>
            </div>
          </div>
          <div ref="messageEnd"></div>
        </div>
        <form class="composer" @submit.prevent="sendMessage">
          <div class="composer-box">
            <textarea
              v-model="draft"
              aria-label="研究问题"
              :disabled="busy || !thread"
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
                :disabled="busy || !thread || !draft.trim()"
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
      :busy="busy"
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
