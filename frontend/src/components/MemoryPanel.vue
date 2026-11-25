<script setup>
import { computed, onMounted, ref } from "vue";
import { FileText, RefreshCw, X } from "@lucide/vue";
import { marked } from "marked";
import { useMemoryText } from "../useMemoryText";
import MemoryTopic from "./MemoryTopic.vue";
import MarkdownContent from "./MarkdownContent.vue";

const props = defineProps({ workspaceId: { type: String, required: true } });
defineEmits(["close"]);
const closeButton = ref(null);
const view = ref("work");
const isWorking = computed(() => view.value === "work");
const root = computed(() => `/workspaces/${props.workspaceId}`);
const { content, loading, error, reload } = useMemoryText(() => isWorking.value
  ? `${root.value}/working-note`
  : `${root.value}/memory/index?scope=${view.value}`);
const topics = computed(() => {
  const entries = [];
  for (const block of marked.lexer(content.value)) {
    if (block.type !== "list" || block.ordered) continue;
    for (const item of block.items) {
      const link = item.tokens[0]?.tokens?.[0];
      if (link?.type !== "link" || !/^[^/\x00]+\.md$/.test(link.href)) continue;
      entries.push({ key: link.href, title: link.text,
        description: item.text.slice(link.raw.length).replace(/^\s*—\s*/, "").trim() });
    }
  }
  return entries;
});
const empty = computed(() => {
  const lines = content.value.trim().split(/\r?\n/).filter(line => line.trim());
  return !lines.length || lines.join("\n") ===
    "# 工作笔记\n## 当前进展与下一步\n## 会话与关键发现\n## 资料与产物";
});

onMounted(() => closeButton.value.focus());
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
        <div class="memory-tabs" role="group" aria-label="记忆类型">
          <button :aria-pressed="isWorking" @click="view = 'work'">工作笔记</button>
          <button :aria-pressed="!isWorking" @click="isWorking && (view = 'workspace')">长期记忆</button>
        </div>
        <button class="icon-button" :aria-label="isWorking ? '刷新工作笔记' : '刷新长期记忆'" :disabled="loading" @click="reload">
          <RefreshCw :size="15" :class="{ spin: loading }" />
        </button>
      </div>
      <div v-if="!isWorking" class="memory-scopes" role="group" aria-label="记忆范围">
        <button class="meta-button" :aria-pressed="view === 'workspace'" @click="view = 'workspace'">当前工作区</button>
        <button class="meta-button" :aria-pressed="view === 'user'" @click="view = 'user'">我的全局记忆</button>
      </div>
      <p class="note-file"><FileText :size="14" />{{ isWorking ? "agent.md" : "memory.md" }}</p>
      <p v-if="loading" class="status-line muted" role="status">正在读取…</p>
      <div v-else-if="error" class="error-message" role="alert">
        {{ error.message }} <button class="text-button" @click="reload">重新读取</button>
      </div>
      <p v-else-if="isWorking && empty" class="muted">暂无工作笔记</p>
      <MarkdownContent v-else-if="isWorking" :content="content" />
      <p v-else-if="!topics.length" class="muted">{{ view === "user" ? "我的全局记忆暂无主题" : "当前工作区暂无长期记忆" }}</p>
      <template v-else>
        <MemoryTopic v-for="topic in topics" :key="`${view}/${topic.key}`" :topic="topic"
          :url="`${root}/memory/topics/${encodeURIComponent(topic.key)}?scope=${view}`" />
      </template>
    </div>
  </aside>
</template>
