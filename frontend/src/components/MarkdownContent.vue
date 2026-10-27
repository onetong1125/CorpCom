<script setup>
import { computed } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";

const props = defineProps({ content: { type: String, required: true } });
// Reports and model output are untrusted HTML at the rendering boundary.
const html = computed(() =>
  DOMPurify.sanitize(marked.parse(props.content), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "iframe"],
    FORBID_ATTR: ["style"],
  }),
);
</script>

<template>
  <div class="markdown" v-html="html"></div>
</template>
