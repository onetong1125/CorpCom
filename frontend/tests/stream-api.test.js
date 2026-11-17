import { afterEach, expect, it, vi } from "vitest";
import { streamRequest } from "../src/api";
import { controlledStream } from "./stream-fixtures";

afterEach(() => vi.unstubAllGlobals());

it("delivers Redis cursors with fragmented frames and retains the cursor on an id-less terminal", async () => {
  const wire = 'id: 123-0\r\ndata: {"type":"started","run_id":"run-1"}\r\n\r\n' +
    'data: {"type":"text_delta","text":"中文"}\r\nid: 123-1\r\n\r\n' +
    ': keepalive\r\n\r\ndata: {"type":"succeeded"}\r\n\r\n';
  const body = new ReadableStream({
    start(controller) {
      for (const byte of new TextEncoder().encode(wire)) controller.enqueue(Uint8Array.of(byte));
    },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
  })));
  const received = [];
  await streamRequest("/stream", {}, (event, id) => received.push([event.type, id]));
  expect(received).toEqual([["started", "123-0"], ["text_delta", "123-1"], ["succeeded", "123-1"]]);
});

it("ignores invalid cursor fields, supports cursor reset and isolates separate streams", async () => {
  const wire = 'id: 123-0\ndata: {"type":"started"}\n\n' +
    'id: bad\0id\ndata: {"type":"text_delta"}\n\n' +
    'id\ndata: {"type":"succeeded"}\n\n';
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(new Response(wire, { headers: { "Content-Type": "text/event-stream" } }))
    .mockResolvedValueOnce(new Response('data: {"type":"succeeded"}\n\n', {
      headers: { "Content-Type": "text/event-stream" },
    })));
  const ids = [];
  await streamRequest("/first", {}, (_, id) => ids.push(id));
  await streamRequest("/second", {}, (_, id) => ids.push(id));
  expect(ids).toEqual(["123-0", "123-0", "", null]);
});

it("decodes fragmented UTF-8, CRLF frames, keepalives and multiline data without waiting for EOF", async () => {
  const cancel = vi.fn();
  const events = [{ type: "text_delta", message_id: "m", text: "中文😀" },
    { type: "succeeded", message: { content: "中文😀" } }];
  const wire = ': ping\r\n\r\ndata: ' + JSON.stringify(events[0]) + '\r\n\r\n' +
    'data: {"type":"succeeded",\r\ndata: "message":{"content":"中文😀"}}\r\n\r\n';
  const body = new ReadableStream({
    start(controller) {
      for (const byte of new TextEncoder().encode(wire)) controller.enqueue(Uint8Array.of(byte));
    }, cancel,
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
  })));
  const received = [];
  expect(await streamRequest("/stream", { content: "测试" }, (event) => received.push(event))).toEqual(events[1]);
  expect(received).toEqual(events);
  expect(cancel).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][1].asResponse).toBeUndefined();
});

it("delivers an increment while the response is still open and then delivers failure", async () => {
  const stream = controlledStream();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(stream.response));
  const receive = vi.fn();
  const result = streamRequest("/stream", {}, receive);
  stream.emit({ type: "text_delta", message_id: "m", text: "部分" });
  await vi.waitFor(() => expect(receive).toHaveBeenCalledOnce());
  stream.emit({ type: "failed", error: "模型错误" });
  expect(await result).toEqual({ type: "failed", error: "模型错误" });
});

it.each([409, 503])("preserves HTTP %s errors before opening a stream", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"拒绝"}', {
    status, headers: { "Content-Type": "application/json" },
  })));
  const receive = vi.fn();
  await expect(streamRequest("/stream", {}, receive)).rejects.toMatchObject({ status });
  expect(receive).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledOnce();
});

it.each(['data: {bad}\n\n', 'data: {"type":"text_delta","text":"部分"}\n\n']) (
  "rejects malformed JSON or EOF without a terminal event: %s", async (wire) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(wire, {
      headers: { "Content-Type": "text/event-stream" },
    })));
    await expect(streamRequest("/stream", {}, vi.fn())).rejects.toThrow();
  },
);

it("passes cancellation to fetch and does not retry", async () => {
  const controller = new AbortController();
  vi.stubGlobal("fetch", vi.fn((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  })));
  const result = streamRequest("/stream", {}, vi.fn(), controller.signal);
  controller.abort();
  await expect(result).rejects.toMatchObject({ name: "AbortError" });
  expect(fetch).toHaveBeenCalledOnce();
});
