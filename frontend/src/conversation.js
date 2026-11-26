export function displayedMessages(thread, run) {
  const messages = thread?.messages || [];
  if (!run?.answer || messages.some((item) => item.id === run.answer.id)) return messages;
  const question = messages.filter((item) => item.role === "user").length === run.userMessageCount
    ? [{ id: `pending:${thread.id}`, role: "user", content: run.content, created_at: run.createdAt }] : [];
  return [...messages, ...question, run.answer];
}

export function pendingText(thread, run) {
  // A refresh may already contain the persisted user message.
  return run?.pending && !run.answer &&
    thread.messages.filter((item) => item.role === "user").length === run.userMessageCount
    ? run.content
    : "";
}

export function conversationEntries(messages, pending, allRuns, currentRun) {
  // Tool calls may persist an assistant message even when the model sent no prose.
  const entries = messages
    .filter((message) => message.role !== "assistant" || message.content.trim())
    .map((message) => ({ type: "message", key: `message:${message.id}`, message }));
  if (pending) entries.push({ type: "pending", key: "pending" });
  for (const run of allRuns) {
    // Preserve event order around saved messages, including a final answer received
    // before the preceding plan has been read back from storage.
    const textIds = new Set(run.items.filter((item) => item.type === "text").map((item) => item.id));
    const firstSaved = entries.findIndex((entry) => entry.type === "message" &&
      textIds.has(entry.message.model_message_id));
    const question = entries.findIndex((entry) => entry.type === "message" &&
      entry.message.id === run.userMessageId);
    let insertAt = firstSaved !== -1 ? firstSaved : question !== -1 ? question + 1 : entries.length;
    let progress = null;
    const lastTool = run.items.findLastIndex((item) => item.type === "tool");
    const completed = run.executionStatus === "succeeded" || run.legacy;
    for (const [index, item] of run.items.entries()) {
      if (item.type === "text") {
        const saved = entries.findIndex((entry) => entry.type === "message" &&
          (entry.message.model_message_id === item.id || entry.message.id === item.savedMessageId));
        if (saved !== -1) {
          // Keep a saved plan between tool calls in its chronological position
          // inside the completed run's disclosure.
          if (completed && progress && index < lastTool) {
            const [plan] = entries.splice(saved, 1);
            progress.items.push({ ...item, text: plan.message.content });
            continue;
          }
          insertAt = saved + 1;
          progress = null;
          continue;
        }
        if (!item.text.trim()) continue;
      }
      if (!progress) {
        progress = { type: "progress", key: `progress:${run.runId ?? run.userMessageId}:${item.type}:${item.id}`, items: [], run };
        entries.splice(insertAt++, 0, progress);
      }
      progress.items.push(item);
    }
    if (run.error && run !== currentRun) {
      entries.splice(insertAt, 0, { type: "run_error", key: `run-error:${run.runId}`, error: run.error });
    }
  }
  return entries;
}
