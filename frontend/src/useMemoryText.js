import { ref, watch } from "vue";
import { request } from "./api";

export function useMemoryText(url) {
  const content = ref("");
  const error = ref(null);
  const loading = ref(false);
  const revision = ref(0);

  watch([url, revision], async ([path], _, onCleanup) => {
    content.value = "";
    error.value = null;
    loading.value = Boolean(path);
    if (!path) return;
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    try {
      const text = await request(path, { asText: true, cache: "no-store", signal: controller.signal });
      if (!controller.signal.aborted) content.value = text;
    } catch (err) {
      if (!controller.signal.aborted) error.value = err;
    } finally {
      if (!controller.signal.aborted) loading.value = false;
    }
  }, { immediate: true });

  return { content, error, loading, reload: () => revision.value++ };
}
