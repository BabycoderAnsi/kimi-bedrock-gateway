"""Mock Amazon Bedrock runtime for offline end-to-end tests.

Implements just enough of ``bedrock-runtime`` for LiteLLM's Converse, ConverseStream,
Invoke and InvokeWithResponseStream calls, and reproduces the Kimi K3 failure modes
documented on the AWS model card / observed in the field, so regressions are caught
without touching AWS:

* tool or output schemas containing regex look-around -> ValidationException
  ("structured output schema uses unsupported regex negative look-ahead ...")
* ``reasoningContent`` from earlier assistant turns -> InternalServerException
* ``cachePoint`` blocks -> ValidationException
* missing/unknown bearer token -> 403 (token value ``mock-valid-token`` is accepted)

Optional behaviours for the auto-mode tests:

* ``--kimi-tool-cmd CMD``: the first Kimi turn of a conversation answers with a Bash
  ``toolUse`` running CMD (later turns, which carry the tool result, answer with text).
* ``--classifier-mode allow|block-rm|404``: how the Claude classifier route answers
  Claude Code's auto-mode classifier prompt (``<block>no</block>`` unless the action
  contains ``rm -rf`` for ``block-rm``; ``404`` simulates a model that is unavailable).

Every request body is appended to ``<log_dir>/requests.jsonl`` with the
Authorization header replaced by a presence flag. Never point real credentials here.

Usage: python tests/mock_bedrock.py --port 4556 --log-dir /tmp/x
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

VALID_TOKEN = "mock-valid-token"
LOOKAROUND = re.compile(r"\(\?<?[=!]")
_lock = threading.Lock()


def _encode_event(event_type: str, payload: dict) -> bytes:
    """Encode one AWS event-stream message (application/vnd.amazon.eventstream)."""

    def header(name: str, value: str) -> bytes:
        n, v = name.encode(), value.encode()
        return struct.pack("B", len(n)) + n + b"\x07" + struct.pack(">H", len(v)) + v

    headers = (
        header(":event-type", event_type)
        + header(":content-type", "application/json")
        + header(":message-type", "event")
    )
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    msg = prelude + prelude_crc + headers + body
    return msg + struct.pack(">I", zlib.crc32(msg) & 0xFFFFFFFF)


def _iter_patterns(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            if key == "patternProperties" and isinstance(value, dict):
                yield from value.keys()
            yield from _iter_patterns(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_patterns(item)


def _schemas_in(body: dict):
    for tool in (body.get("toolConfig") or {}).get("tools", []):
        spec = tool.get("toolSpec") or {}
        yield (spec.get("inputSchema") or {}).get("json")
    for tool in body.get("tools") or []:  # Invoke / chat-completions shapes
        yield tool.get("input_schema") or (tool.get("function") or {}).get("parameters")
    fmt = ((body.get("outputConfig") or {}).get("textFormat") or {}).get("structure") or {}
    raw = (fmt.get("jsonSchema") or {}).get("schema")
    if isinstance(raw, str):
        try:
            yield json.loads(raw)
        except ValueError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "MockBedrock/1.0"
    log_dir = "."
    kimi_tool_cmd: str | None = None
    classifier_mode = "allow"

    def log_message(self, *args):  # keep test output quiet
        pass

    def _record(self, model: str, op: str, body):
        auth = self.headers.get("authorization", "")
        entry = {
            "model": model,
            "op": op,
            "auth": "bearer" if auth.lower().startswith("bearer ") else ("sigv4" if auth else "none"),
            "token_valid": auth == f"Bearer {VALID_TOKEN}",
            "body": body,
        }
        with _lock, open(os.path.join(self.log_dir, "requests.jsonl"), "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def _error(self, status: int, err_type: str, message: str):
        payload = json.dumps({"message": message}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("x-amzn-errortype", f"{err_type}:http://internal.amazon.com/coral/com.amazon.bedrock/")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._error(404, "ResourceNotFoundException", "not implemented in mock")

    def do_POST(self):
        if self.path.split("?")[0] == "/openai/v1/chat/completions":
            return self._chat_completions()
        m = re.match(r"^/model/(?P<model>[^/]+)/(?P<op>converse-stream|converse|invoke-with-response-stream|invoke)$",
                     self.path)
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        if not m:
            return self._error(404, "UnknownOperationException", "unknown path")
        model, op = unquote(m["model"]), m["op"]
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._error(400, "ValidationException", "malformed JSON")
        entry = self._record(model, op, body)

        if not entry["token_valid"]:
            return self._error(403, "AccessDeniedException",
                               "Authentication failed: Please make sure your API Key is valid.")
        if "nonexistent" in model:
            return self._error(400, "ValidationException", "The provided model identifier is invalid.")
        if "noaccess" in model:
            return self._error(403, "AccessDeniedException",
                               "You don't have access to the model with the specified model ID.")

        is_kimi = "moonshotai.kimi" in model
        if is_kimi:
            for schema in _schemas_in(body):
                for pattern in _iter_patterns(schema or {}):
                    if LOOKAROUND.search(pattern):
                        return self._error(
                            400, "ValidationException",
                            "structured output schema uses unsupported regex negative look-ahead; "
                            "replace look-around assertions with a regular expression using supported constructs")
            blob = json.dumps(body)
            if '"cachePoint"' in blob:
                return self._error(400, "ValidationException", "This model doesn't support the cachePoint field")
            for msg in (body.get("messages") or [])[:-1]:
                if msg.get("role") == "assistant" and any(
                        isinstance(c, dict) and "reasoningContent" in c for c in msg.get("content") or []):
                    return self._error(500, "InternalServerException",
                                       "The server encountered an internal error while processing the request.")

        text = f"mock reply from {model}"
        if op in ("converse", "converse-stream"):
            msgs = body.get("messages") or []
            last = msgs[-1].get("content", []) if msgs else []
            carries_result = any(isinstance(c, dict) and "toolResult" in c for c in last)
            if is_kimi and self.kimi_tool_cmd and body.get("toolConfig") and not carries_result:
                return self._converse_tool(op, self.kimi_tool_cmd)
            return self._converse(op, text)
        blob = json.dumps(body)
        if "security monitor for autonomous AI coding agents" in blob:
            if self.classifier_mode == "404":
                return self._error(404, "ResourceNotFoundException", "Model not found (mock).")
            transcript = json.dumps((body.get("messages") or [{}])[-1])
            if self.classifier_mode == "block-rm" and "rm -rf" in transcript:
                text = ("<block>yes</block><category>Irreversible Local Destruction</category>"
                        "<reason>[Irreversible Local Destruction] mock classifier blocked rm -rf</reason>")
            else:
                text = "<block>no</block>"
        return self._invoke(op, model, text)

    # ------------------------------------------------- OpenAI-compatible Chat Completions
    def _openai_error(self, status: int, err_type: str, message: str, code: str):
        payload = json.dumps({"error": {"message": message, "type": err_type, "param": None, "code": code}}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _chat_completions(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._openai_error(400, "invalid_request_error", "malformed JSON", "validation_error")
        model = str(body.get("model", ""))
        entry = self._record(model, "chat-completions", body)
        if not entry["token_valid"]:
            return self._openai_error(401, "authentication_error", "Invalid API key.", "invalid_api_key")
        if "moonshotai.kimi" in model:
            for tool in body.get("tools") or []:
                for pattern in _iter_patterns((tool.get("function") or {}).get("parameters") or {}):
                    if LOOKAROUND.search(pattern):
                        return self._openai_error(
                            400, "invalid_request_error",
                            "structured output schema uses unsupported regex negative look-ahead; replace "
                            "look-around assertions with a regular expression using supported constructs",
                            "validation_error")
            if any(m.get("role") == "assistant" and m.get("reasoning_content") for m in body.get("messages") or []):
                return self._openai_error(500, "server_error", "The server had an error while processing your "
                                                               "request. Sorry about that!", "internal_server_error")
        msgs = body.get("messages") or []
        # Propose the tool call once per conversation: any earlier assistant tool call (allowed or denied)
        # means this turn answers with text, so Claude Code's loop always terminates.
        already_called = any(m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls"))
                             for m in msgs)
        tool_turn = bool(self.kimi_tool_cmd and body.get("tools") and not already_called)
        effort = body.get("reasoning_effort")
        reasoning = "" if effort == "none" else f"(mock reasoning at effort {effort})"
        text = f"mock reply from {model}"
        usage = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
        call = {"id": "call_mock1", "type": "function",
                "function": {"name": "Bash", "arguments": json.dumps({"command": self.kimi_tool_cmd or "",
                                                                      "description": "Mock probe command"})}}
        if not body.get("stream"):
            message = {"role": "assistant", "content": None if tool_turn else text}
            if reasoning:
                message["reasoning_content"] = reasoning
            if tool_turn:
                message["tool_calls"] = [call]
            payload = json.dumps({"id": "chatcmpl-mock", "object": "chat.completion", "model": model,
                                  "choices": [{"index": 0, "message": message,
                                               "finish_reason": "tool_calls" if tool_turn else "stop"}],
                                  "usage": usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def chunk(delta, finish=None, extra=None):
            d = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            d.update(extra or {})
            self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        if reasoning:
            chunk({"reasoning_content": reasoning})
        if tool_turn:
            chunk({"tool_calls": [{"index": 0, **call}]})
            chunk({}, "tool_calls")
        else:
            chunk({"content": text})
            chunk({}, "stop")
        self.wfile.write(f"data: {json.dumps({'id': 'chatcmpl-mock', 'object': 'chat.completion.chunk', 'model': model, 'choices': [], 'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _converse_tool(self, op: str, command: str):
        tool_input = json.dumps({"command": command, "description": "Mock probe command"})
        if op == "converse":
            payload = json.dumps({
                "output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "tooluse_mock1", "name": "Bash", "input": json.loads(tool_input)}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 12, "outputTokens": 9, "totalTokens": 21},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._stream([
            ("messageStart", {"role": "assistant"}),
            ("contentBlockStart", {"contentBlockIndex": 0,
                                   "start": {"toolUse": {"toolUseId": "tooluse_mock1", "name": "Bash"}}}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": tool_input}}}),
            ("contentBlockStop", {"contentBlockIndex": 0}),
            ("messageStop", {"stopReason": "tool_use"}),
            ("metadata", {"usage": {"inputTokens": 12, "outputTokens": 9, "totalTokens": 21},
                          "metrics": {"latencyMs": 5}}),
        ])

    def _converse(self, op: str, text: str):
        usage = {"inputTokens": 12, "outputTokens": 5, "totalTokens": 17}
        if op == "converse":
            payload = json.dumps({
                "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                "stopReason": "end_turn", "usage": usage, "metrics": {"latencyMs": 5},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        events = [
            ("messageStart", {"role": "assistant"}),
            ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}),
            ("contentBlockStop", {"contentBlockIndex": 0}),
            ("messageStop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": usage, "metrics": {"latencyMs": 5}}),
        ]
        self._stream(events)

    def _invoke(self, op: str, model: str, text: str):
        # Anthropic-on-Bedrock Messages shape (used for the Claude classifier route).
        message = {
            "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }
        if op == "invoke":
            payload = json.dumps(message).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        chunks = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop", "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": 12, "outputTokenCount": 5, "invocationLatency": 5, "firstByteLatency": 2}},
        ]
        events = [("chunk", {"bytes": base64.b64encode(json.dumps(c).encode()).decode()}) for c in chunks]
        self._stream(events)

    def _stream(self, events):
        self.send_response(200)
        self.send_header("content-type", "application/vnd.amazon.eventstream")
        self.end_headers()
        for name, payload in events:
            self.wfile.write(_encode_event(name, payload))
            self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4556)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--kimi-tool-cmd")
    parser.add_argument("--classifier-mode", choices=["allow", "block-rm", "404"], default="allow")
    args = parser.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    Handler.log_dir = args.log_dir
    Handler.kimi_tool_cmd = args.kimi_tool_cmd
    Handler.classifier_mode = args.classifier_mode
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
