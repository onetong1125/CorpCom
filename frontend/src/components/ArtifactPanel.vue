<script setup>
import { computed, ref, watch } from "vue";
import {
  Download,
  FileText,
  Maximize2,
  Minimize2,
  ArrowLeft,
  Upload,
  RefreshCw,
} from "@lucide/vue";
import { request, fileUrl } from "../api";
import MarkdownContent from "./MarkdownContent.vue";
import ArtifactTree from "./ArtifactTree.vue";

const props = defineProps({
  workspace: { type: Object, required: true },
  busy: Boolean,
});
const emit = defineEmits(["upload"]);
const selected = ref(null);
const content = ref("");
const loading = ref(false);
const error = ref("");
const expanded = ref(false);
const fileInput = ref(null);
const tree = computed(() => {
  const roots = [];
  for (const file of props.workspace.artifacts) {
    const parts = file.relative_path
      .replace(`workspaces/${props.workspace.id}/`, "")
      .split("/");
    let children = roots;
    let path = "";
    for (const name of parts.slice(0, -1)) {
      path = path ? `${path}/${name}` : name;
      let folder = children.find((node) => node.children && node.name === name);
      if (!folder) {
        folder = { name, path, children: [] };
        children.push(folder);
      }
      children = folder.children;
    }
    children.push({ name: file.name, path: file.id, file });
  }
  return roots;
});
const isMarkdown = computed(() =>
  /\.(md|markdown)$/i.test(selected.value?.name || ""),
);
const isText = computed(() =>
  /\.(md|markdown|txt|json|csv|tsv|py|js|ts|yaml|yml|log)$/i.test(
    selected.value?.name || "",
  ),
);

watch(selected, async (file, _, onCleanup) => {
  content.value = "";
  error.value = "";
  loading.value = false;
  if (!file || !isText.value) return;
  const controller = new AbortController();
  onCleanup(() => controller.abort());
  loading.value = true;
  try {
    content.value = await request(
      `/workspaces/${props.workspace.id}/files/${file.id}/content`,
      {
        asText: true,
        signal: controller.signal,
      },
    );
  } catch (err) {
    if (err.name !== "AbortError") error.value = err.message;
  } finally {
    if (!controller.signal.aborted) loading.value = false;
  }
});

function upload(event) {
  const file = event.target.files[0];
  if (file) emit("upload", file);
  event.target.value = "";
}
</script>

<template>
  <aside class="artifact-panel" :class="{ expanded }" aria-label="研究产物">
    <header class="panel-header">
      <div>
        <span class="eyebrow">RESEARCH LIBRARY</span>
        <h2>
          研究产物 <span class="count">{{ workspace.artifacts.length }}</span>
        </h2>
      </div>
      <div class="inline-actions">
        <button
          class="icon-button"
          title="上传资料"
          aria-label="上传资料"
          :disabled="busy"
          @click="fileInput.click()"
        >
          <Upload :size="17" />
        </button>
        <button
          class="icon-button"
          :aria-label="expanded ? '收起产物面板' : '展开产物面板'"
          @click="expanded = !expanded"
        >
          <Minimize2 v-if="expanded" :size="17" /><Maximize2
            v-else
            :size="17"
          />
        </button>
      </div>
      <input
        ref="fileInput"
        type="file"
        hidden
        aria-label="选择上传文件"
        @change="upload"
      />
    </header>
    <div v-if="selected" class="file-preview">
      <div class="preview-toolbar">
        <button class="text-button" @click="selected = null">
          <ArrowLeft :size="15" />文件列表
        </button>
        <a
          class="text-button"
          :href="fileUrl(workspace.id, selected.id)"
          :download="selected.name"
          ><Download :size="15" />下载</a
        >
      </div>
      <div class="preview-heading">
        <h3>{{ selected.name }}</h3>
        <p>
          {{
            selected.relative_path.replace(`workspaces/${workspace.id}/`, "")
          }}
        </p>
      </div>
      <p v-if="loading" class="muted status-line" role="status">
        <RefreshCw class="spin" :size="16" />正在读取文件…
      </p>
      <div v-else-if="error" class="error-message" role="alert">
        {{ error
        }}<button class="text-button" @click="selected = { ...selected }">
          重新读取
        </button>
      </div>
      <template v-else-if="isText">
        <MarkdownContent v-if="isMarkdown" :content="content" />
        <pre v-else class="source-preview">{{ content }}</pre>
      </template>
      <div v-else class="empty-small">
        <FileText :size="28" />
        <p>此格式暂不支持在线预览</p>
        <a
          :href="fileUrl(workspace.id, selected.id)"
          :download="selected.name"
          class="text-button"
          >下载查看</a
        >
      </div>
    </div>
    <!-- Keep native disclosure state when opening a file and returning to the tree. -->
    <div v-show="!selected" class="artifact-list">
      <div v-if="!tree.length" class="empty-small">
        <FileText :size="32" />
        <h3>研究从证据开始</h3>
        <p>生成报告后，数据与代码会保存在这里。也可以先上传已有资料。</p>
        <button
          class="secondary-button"
          :disabled="busy"
          @click="fileInput.click()"
        >
          上传资料
        </button>
      </div>
      <ArtifactTree :nodes="tree" @select="selected = $event" />
    </div>
  </aside>
</template>
