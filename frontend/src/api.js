export class StreamDisconnectedError extends Error {}

// Same-origin requests go through Vite's proxy; model credentials stay on the backend.
export async function request(path, options = {}) {
  const { asText, asResponse, ...fetchOptions } = options;
  let response;
  try {
    response = await fetch(`/api${path}`, fetchOptions);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new StreamDisconnectedError("无法连接后端，请检查服务是否启动。", { cause: error });
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
  return readEventStream(response, onEvent);
}

export async function replayRequest(path, after, onEvent, signal) {
  const response = await request(`${path}?after=${encodeURIComponent(after)}`, {
    signal, asResponse: true,
  });
  return readEventStream(response, onEvent);
}

async function readEventStream(response, onEvent) {
  if (!response.headers.get("content-type")?.startsWith("text/event-stream"))
    throw new Error("后端未返回研究事件流，请检查后端版本。");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let data = [];
  let lastEventId = null;
  // The server sends LF or CRLF-delimited SSE, including keepalive comments.
  const iterator = response.body[Symbol.asyncIterator]();
  let readingFailed = false;
  try {
    while (true) {
      let chunk;
      try {
        chunk = await iterator.next();
      } catch (error) {
        readingFailed = true;
        if (error.name === "AbortError") throw error;
        throw new StreamDisconnectedError("事件连接已中断。", { cause: error });
      }
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        if (line === "" && data.length) {
          const event = JSON.parse(data.join("\n"));
          data = [];
          onEvent(event, lastEventId);
          if (["succeeded", "failed", "run_finished"].includes(event.type)) return event;
        } else if (line.startsWith("data:")) {
          data.push(line.slice(5).replace(/^ /, ""));
        } else if (line === "id" || line.startsWith("id:")) {
          const value = line === "id" ? "" : line.slice(3).replace(/^ /, "");
          if (!value.includes("\0")) lastEventId = value;
        }
      }
    }
    decoder.decode();
    throw new StreamDisconnectedError("事件连接已中断，研究可能仍在后台进行，正在恢复连接。");
  } finally {
    if (!readingFailed) await iterator.return();
  }
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
