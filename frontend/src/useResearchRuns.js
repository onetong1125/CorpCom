import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { request, streamRequest, replayRequest, StreamDisconnectedError } from "./api";
import { applyRunEvent } from "./researchRunState";
import * as conversation from "./conversation";

// pending covers delivery/history loading through the final refresh; reconciling
// keeps polling alive after a delivery error until storage has been checked.
export function useResearchRuns({ workspace, thread, refreshResearch }) {
  const runs = reactive({});
  const runHistory = reactive({});
  const legacyHistory = reactive({});
  const drafts = reactive({});
  const pollError = ref("");
  const connections = new Set();
  let disposed = false;
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
  const displayedMessages = computed(() => conversation.displayedMessages(thread.value, currentRun.value));
  const pendingText = computed(() => conversation.pendingText(thread.value, currentRun.value));
  const conversationEntries = computed(() => conversation.conversationEntries(
    displayedMessages.value, pendingText.value,
    [...(legacyHistory[thread.value?.id] || []), ...(runHistory[thread.value?.id] || []), currentRun.value].filter(Boolean),
    currentRun.value,
  ));
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
  function reconcileThread(detail) {
    const run = runs[detail.id];
    if (run && run.userMessageId == null) {
      run.userMessageId = detail.messages.filter((message) => message.role === "user")[run.userMessageCount]?.id ?? null;
    }
    if (!run || (!run.reconciling && run.connectionStatus !== "disconnected")) return;
    run.reconciling = false;
    if (run.connectionStatus === "disconnected" &&
        detail.messages.filter((item) => item.role === "user").length > run.userMessageCount &&
        ["succeeded", "failed", "interrupted"].includes(detail.execution_status)) {
      run.executionStatus = detail.execution_status;
      run.error = detail.execution_status === "succeeded" ? "" : detail.execution_error;
    }
  }

  function reconcileWorkspace(detail, selectedThreadId) {
    // Non-selected threads are observed through the workspace status list.
    for (const item of detail.threads) {
      if (item.id !== selectedThreadId && runs[item.id]) runs[item.id].reconciling = false;
    }
  }

  async function loadHistory(id, detail) {
    const records = !runs[detail.id]
      ? await request(`/workspaces/${id}/threads/${detail.id}/runs`)
      : null;
    const recordedQuestions = new Set(records?.map((record) => record.user_message_id));
    const missingQuestions = records !== null && detail.messages.some((message) =>
      message.role === "user" && !recordedQuestions.has(message.id));
    const history = missingQuestions && !legacyHistory[detail.id]
      ? await request(`/workspaces/${id}/threads/${detail.id}/history`)
      : null;
    // Navigation can load this thread again while these requests are in flight.
    if (disposed || runs[detail.id]) return;
    if (history) {
      if (history.thread_id !== detail.id) throw new Error("研究历史归属不匹配。");
      legacyHistory[detail.id] = history.turns
        .filter((turn) => !recordedQuestions.has(turn.user_message_id))
        .map((turn) => {
          const start = detail.messages.findIndex((message) => message.id === turn.user_message_id);
          const following = detail.messages.slice(start + 1);
          const nextQuestion = following.findIndex((message) => message.role === "user");
          const savedAnswers = (nextQuestion === -1 ? following : following.slice(0, nextQuestion))
            .filter((message) => message.role === "assistant");
          const usedAnswers = new Set();
          const state = { userMessageId: turn.user_message_id, legacy: true, executionStatus: null, connectionStatus: "idle", pending: false, items: [] };
          for (const event of turn.events) applyRunEvent(id, detail.id, state, event);
          // Early saved answers have no model ID; match exact text only within this question.
          for (const item of state.items.filter((item) => item.type === "text")) {
            const saved = savedAnswers.find((message) => !usedAnswers.has(message.id) &&
              (message.model_message_id === item.id || (!message.model_message_id && message.content === item.text)));
            if (saved) {
              item.savedMessageId = saved.id;
              usedAnswers.add(saved.id);
            }
          }
          return state;
        });
    }
    if (records?.length) restoreRunHistory(id, detail, records);
  }

  async function restoreRunHistory(workspaceId, detail, records) {
    const restored = records.map((record) => {
      const questionIndex = detail.messages.findIndex((message) => message.id === record.user_message_id);
      const question = detail.messages[questionIndex];
      return {
        pending: true,
        content: question?.content || "",
        createdAt: record.started_at,
        userMessageId: record.user_message_id,
        userMessageCount: (questionIndex === -1 ? detail.messages : detail.messages.slice(0, questionIndex))
          .filter((message) => message.role === "user").length,
        error: "",
        executionStatus: record.status,
        connectionStatus: "idle",
        runId: record.id,
        lastEventId: null,
        items: [],
        answer: null,
        reconciling: false,
      };
    });
    runHistory[detail.id] = restored.slice(0, -1);
    runs[detail.id] = restored.at(-1);
    // Rebuild empty page state from the start; keep each run's cursor separate.
    for (let index = 0; index < restored.length; index += 1) {
      if (disposed) return;
      const run = index === restored.length - 1 ? runs[detail.id] : runHistory[detail.id][index];
      if (run.executionStatus === "running") {
        await followRun(workspaceId, detail.id, run, true);
      } else {
        const controller = new AbortController();
        connections.add(controller);
        try {
          const history = await request(
            `/workspaces/${workspaceId}/threads/${detail.id}/runs/${run.runId}/history`,
            { signal: controller.signal },
          );
          if (history.run.id !== run.runId || history.run.thread_id !== detail.id)
            throw new Error("研究历史归属不匹配。");
          for (const event of history.events) applyRunEvent(workspaceId, detail.id, run, event);
          run.executionStatus = history.run.status;
          run.error = history.run.error || "";
        } catch (err) {
          if (disposed) return;
          run.error = `读取研究历史失败：${err.message}`;
        } finally {
          connections.delete(controller);
          run.pending = false;
        }
      }
    }
  }

  async function send(workspaceId, detail, content) {
    const threadId = detail.id;
    if (runs[threadId]) {
      (runHistory[threadId] ||= []).push(runs[threadId]);
    }
    runs[threadId] = {
      pending: true,
      content,
      createdAt: new Date().toISOString(),
      userMessageCount: detail.messages.filter((item) => item.role === "user")
        .length,
      error: "",
      executionStatus: null,
      connectionStatus: "connecting",
      runId: null,
      lastEventId: null,
      items: [],
      answer: null,
      reconciling: false,
    };
    await followRun(workspaceId, threadId, runs[threadId], false);
  }

  async function followRun(workspaceId, threadId, run, recovering) {
    const controller = new AbortController();
    connections.add(controller);
    run.connectionStatus = "connecting";
    const applyEvent = (event, eventId) => {
      if (run.connectionStatus === "reconnecting") run.error = "";
      applyRunEvent(workspaceId, threadId, run, event, eventId);
      run.connectionStatus = "connected";
    };
    try {
      let resume = recovering;
      while (true) {
        try {
          if (resume) {
            await replayRequest(
              `/workspaces/${workspaceId}/threads/${threadId}/runs/${run.runId}/events`,
              run.lastEventId || "0-0", applyEvent, controller.signal,
            );
          } else {
            await streamRequest(
              `/workspaces/${workspaceId}/threads/${threadId}/messages/stream`,
              { content: run.content }, applyEvent, controller.signal,
            );
          }
          break;
        } catch (err) {
          if (disposed || !(err instanceof StreamDisconnectedError) || !run.runId) throw err;
          run.connectionStatus = "reconnecting";
          run.error = "连接已中断，正在恢复本轮研究输出。";
          // Retry only the existing run's GET stream, never the question POST.
          resume = true;
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (disposed) return;
        }
      }
      run.connectionStatus = "closed";
      if (run.executionStatus === "succeeded") {
        run.error = "";
        if (!recovering) drafts[threadId] = "";
      }
    } catch (err) {
      if (disposed) return;
      run.connectionStatus = err.status ? "rejected" : "disconnected";
      run.reconciling = true;
      const detail = err.status ? err.message : `事件连接中断，研究可能仍在后台进行。${err.message}`;
      run.error = `${detail} 请先核对消息记录；重新发送会发起新的请求。`;
    } finally {
      connections.delete(controller);
    }
    if (disposed) return;
    try {
      if (run === runs[threadId]) await refreshResearch(workspaceId);
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

  onBeforeUnmount(() => {
    disposed = true;
    for (const controller of connections) controller.abort();
  });

  return {
    draft, sending, currentRun, displayedMessages, pendingText, conversationEntries,
    executionError, pollError, executionLabel,
    send, loadHistory, reconcileThread, reconcileWorkspace,
  };
}
