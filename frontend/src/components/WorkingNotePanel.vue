<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { FileText, RefreshCw, X } from "@lucide/vue";
import { request } from "../api";
import MarkdownContent from "./MarkdownContent.vue";

const props = defineProps({ workspaceId: { type: String, required: true } });
defineEmits(["close"]);
const closeButton = ref(null);
const content = ref("");
const loading = ref(false);
const error = ref("");
const controller = new AbortController();
const empty = computed(() => {
  const lines = content.value.trim().split(/\r?\n/).filter(line => line.trim());
  return !lines.length || lines.join("\n") ===
    "# 工作笔记\n## 当前进展与下一步\n## 会话与关键发现\n## 资料与产物";
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    content.value = await request(`/workspaces/${props.workspaceId}/working-note`, {
      asText: true, signal: controller.signal, cache: "no-store",
    });
  } catch (err) {
    if (!controller.signal.aborted) error.value = err.message;
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  closeButton.value.focus();
  load();
});
onUnmounted(() => controller.abort());
</script>

<template>
  <aside id="memory-panel" class="artifact-panel memory-panel" aria-label="记忆" @keydown.esc="$emit('close')">
    <header class="panel-header">
      <h2>记忆</h2>
      <button ref="closeButton" class="icon-button" aria-label="关闭记忆" @click="$emit('close')">
        <X :size="18" />
      </button>
    </header>
    <div class="artifact-list">
      <div class="note-heading">
        <h3>工作笔记</h3>
        <button class="icon-button" aria-label="刷新工作笔记" title="刷新工作笔记" :disabled="loading" @click="load">
          <RefreshCw :size="15" :class="{ spin: loading }" />
        </button>
      </div>
      <p class="note-file"><FileText :size="14" />agent.md</p>
      <p v-if="loading" class="status-line muted" role="status">正在读取工作笔记…</p>
      <div v-else-if="error" class="error-message" role="alert">
        {{ error }} <button class="text-button" @click="load">重新读取</button>
      </div>
      <p v-else-if="empty" class="muted">暂无工作笔记</p>
      <MarkdownContent v-else :content="content" />
    </div>
  </aside>
</template>
