<script setup>
import { computed } from "vue";
import { Marked } from "marked";
import DOMPurify from "dompurify";

const props = defineProps({ content: { type: String, required: true } });
// Reports and model output are untrusted HTML at the rendering boundary.
const markdown = new Marked({
  renderer: { checkbox: ({ checked }) => checked ? "☑ " : "☐ " },
});
const html = computed(() =>
  DOMPurify.sanitize(markdown.parse(props.content), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "iframe"],
    FORBID_ATTR: ["style"],
  }),
);
</script>

<template>
  <div class="markdown" v-html="html"></div>
</template>
