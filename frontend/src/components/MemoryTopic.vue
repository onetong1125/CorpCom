<script setup>
import { computed, ref } from "vue";
import { ChevronRight } from "@lucide/vue";
import MarkdownContent from "./MarkdownContent.vue";
import { useMemoryText } from "../useMemoryText";

const props = defineProps({
  topic: { type: Object, required: true },
  url: { type: String, required: true },
});
const expanded = ref(false);
const { content, error, loading, reload } = useMemoryText(() => expanded.value ? props.url : null);
const body = computed(() => content.value.replace(/^---[ \t]*\r?\n(?:[^\n]*\n)*?---[ \t]*(?:\r?\n|$)/, ""));
</script>

<template>
  <article class="memory-topic">
    <button class="topic-title" :aria-expanded="expanded" @click="expanded = !expanded">
      <ChevronRight :size="16" />{{ topic.title }}
    </button>
    <p v-if="topic.description" class="topic-description">{{ topic.description }}</p>
    <div v-if="expanded">
      <code class="note-file">{{ topic.key }}</code>
      <p v-if="loading" role="status" class="muted">正在读取主题…</p>
      <div v-else-if="error" role="alert" class="error-message">
        {{ error.status === 404 ? `找不到主题文件 ${topic.key}` : error.message }}
        <button class="text-button" @click="reload">重新读取</button>
      </div>
      <MarkdownContent v-else :content="body" />
    </div>
  </article>
</template>
