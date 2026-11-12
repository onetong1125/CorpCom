// Same-origin requests go through Vite's proxy; model credentials stay on the backend.
export async function request(path, options = {}) {
  const { asText, asResponse, ...fetchOptions } = options;
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
    const error = new Error(
      `请求失败（${response.status}）：${detail || response.statusText}`,
    );
    error.status = response.status;
    throw error;
  }
  if (asResponse) return response;
  return asText ? response.text() : response.json();
}

export async function streamRequest(path, body, onEvent, signal) {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
    asResponse: true,
  });
  if (!response.headers.get("content-type")?.startsWith("text/event-stream"))
    throw new Error("后端未返回研究事件流，请检查后端版本。");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let data = [];
  // The server sends LF or CRLF-delimited SSE, including keepalive comments.
  // Async iteration cancels the body when a terminal event or parsing error ends it.
  for await (const bytes of response.body) {
    buffer += decoder.decode(bytes, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (line === "" && data.length) {
        const event = JSON.parse(data.join("\n"));
        data = [];
        onEvent(event);
        if (event.type === "succeeded" || event.type === "failed") return event;
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
      }
    }
  }
  decoder.decode();
  throw new Error("事件连接已中断，研究可能仍在后台进行，正在查询运行状态。");
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
