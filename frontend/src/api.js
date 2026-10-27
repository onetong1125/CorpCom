// Same-origin requests go through Vite's proxy; model credentials stay on the backend.
export async function request(path, options = {}) {
  const { asText, ...fetchOptions } = options;
  let response;
  try {
    response = await fetch(`/api${path}`, fetchOptions);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("无法连接后端，请检查服务是否启动。", { cause: error });
  }
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    if (response.headers.get("content-type")?.includes("application/json")) {
      const payload = JSON.parse(text);
      detail = Array.isArray(payload.detail)
        ? payload.detail
            .map((item) => `${item.loc.join(".")}: ${item.msg}`)
            .join("；")
        : payload.detail;
    }
    throw new Error(
      `请求失败（${response.status}）：${detail || response.statusText}`,
    );
  }
  return asText ? response.text() : response.json();
}

export function jsonRequest(path, body, method = "POST") {
  return request(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fileUrl(workspaceId, artifactId) {
  return `/api/workspaces/${workspaceId}/files/${artifactId}/content`;
}
