// executionStatus is backend state (or null before admission).
// connectionStatus describes only delivery: idle, connecting, connected,
// reconnecting, disconnected, rejected, or closed.
export function applyRunEvent(workspaceId, threadId, run, event, eventId = null) {
  switch (event.type) {
    case "started":
      if (event.workspace_id !== workspaceId || event.thread_id !== threadId)
        throw new Error("研究事件归属不匹配。");
      run.executionStatus = "running";
      run.runId = event.run_id;
      break;
    case "assistant_message":
    case "text_delta": {
      let item = run.items.find((item) => item.type === "text" && item.id === event.message_id);
      if (!item) {
        run.items.push({ type: "text", id: event.message_id, text: "" });
        item = run.items.at(-1);
      }
      if (event.type === "text_delta") item.text += event.text;
      else {
        item.text = event.text;
        for (const call of event.tool_calls) {
          run.items.push({
            type: "tool", id: call.id, toolCallId: call.id,
            name: call.name, args: call.args, status: "running",
          });
        }
      }
      break;
    }
    case "tool_start":
      run.items.push({ type: "tool", id: event.tool_run_id, toolCallId: event.tool_call_id, name: event.name, status: "running" });
      break;
    case "tool_result":
    case "tool_end":
    case "tool_error": {
      const item = run.items.find((item) => item.type === "tool" &&
        (event.tool_call_id ? item.toolCallId === event.tool_call_id : item.id === event.tool_run_id));
      item.status = event.type === "tool_error" || event.status === "error" ? "error" : "returned";
      item.error = event.error;
      item.content = event.content;
      break;
    }
    case "succeeded":
      run.executionStatus = "succeeded";
      run.answer = event.message;
      break;
    case "failed":
      run.executionStatus = "failed";
      run.error = `研究失败：${event.error || "后端执行失败"} 请核对消息记录后再决定是否重发。`;
      break;
    case "run_finished":
      if (event.run_id !== run.runId) throw new Error("研究事件归属不匹配。");
      run.executionStatus = event.status;
      run.error = event.status === "succeeded" ? "" : event.error;
      break;
    default:
      throw new Error(`无法识别研究事件：${event.type}`);
  }
  // Advance only after the event has been applied to this run.
  if (eventId !== null) run.lastEventId = eventId;
}

