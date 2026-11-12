export function controlledStream() {
  let controller;
  const encoder = new TextEncoder();
  const response = new Response(new ReadableStream({
    start(value) { controller = value; },
  }), { headers: { "Content-Type": "text/event-stream" } });
  return {
    response,
    emit(event) { controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`)); },
    close() { controller.close(); },
    fail(error) { controller.error(error); },
  };
}

export function successResponse(message, workspaceId, threadId) {
  return new Response([
    { type: "started", workspace_id: workspaceId, thread_id: threadId },
    { type: "text_delta", message_id: "model-answer", text: message.content },
    { type: "succeeded", message: { ...message, thread_id: threadId } },
  ].map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), {
    headers: { "Content-Type": "text/event-stream" },
  });
}
