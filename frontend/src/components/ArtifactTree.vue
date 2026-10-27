<script setup>
import { computed } from "vue";
import { ChevronRight, Folder, FileText, FileCode2 } from "@lucide/vue";

const props = defineProps({ nodes: { type: Array, required: true } });
const emit = defineEmits(["select"]);
const sorted = computed(() =>
  [...props.nodes].sort((a, b) => {
    if (Boolean(a.children) !== Boolean(b.children)) return a.children ? -1 : 1;
    return a.children
      ? b.name.localeCompare(a.name, "zh-CN", { numeric: true })
      : a.name.localeCompare(b.name, "zh-CN", { numeric: true });
  }),
);
</script>

<template>
  <div class="artifact-tree">
    <template v-for="node in sorted" :key="node.path">
      <details
        v-if="node.children"
        class="artifact-directory"
        :data-path="node.path"
      >
        <summary :title="node.path">
          <ChevronRight class="directory-chevron" :size="14" />
          <Folder :size="15" />
          <span>{{ node.name }}</span>
        </summary>
        <ArtifactTree :nodes="node.children" @select="emit('select', $event)" />
      </details>
      <button
        v-else
        class="file-row"
        :title="node.name"
        @click="emit('select', node.file)"
      >
        <span class="file-icon" :class="{ report: /\.md$/i.test(node.name) }">
          <FileText v-if="/\.md$/i.test(node.name)" :size="19" />
          <FileCode2 v-else :size="19" />
        </span>
        <span class="file-info">
          <strong>{{ node.name }}</strong>
          <small
            >{{ node.file.kind }} ·
            {{
              new Date(node.file.created_at).toLocaleDateString("zh-CN")
            }}</small
          >
        </span>
        <span class="file-arrow">↗</span>
      </button>
    </template>
  </div>
</template>
