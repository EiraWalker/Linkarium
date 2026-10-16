"""Loopback-only Chat Completions fixture for real UI/HTTP acceptance.

This is a deterministic protocol fixture, not an AI model or a production server.
It never logs request bodies, authorization headers or page text.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 256 * 1024:
            self.send_error(413)
            return
        if self.headers.get("Authorization") != "Bearer ui-fixture-key":
            self.send_error(401)
            return
        try:
            request = json.loads(self.rfile.read(size))
            messages = request["messages"]
            user = next(message["content"] for message in reversed(messages) if message["role"] == "user")
            try:
                payload = json.loads(user)
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and payload.get("task") == "plan_taxonomy":
                result = {"categories": [{"path": ["AI 协议验收", "待校对"], "description": "Protocol fixture websites"}]}
            elif isinstance(payload, dict) and "websites" in payload:
                result = {"items": [{"bookmark_id": row["bookmark_id"],
                    "target": payload["categories"][0]["target"],
                    "reason": "受控协议样本：验证建议须人工确认后才创建目录。"}
                    for row in payload["websites"]]}
            else:
                result = {"ok": True}
            body = json.dumps({"choices": [{"message": {"role": "assistant",
                "content": json.dumps(result, ensure_ascii=False)}, "finish_reason": "stop"}]}, ensure_ascii=False).encode()
        except (ValueError, KeyError, TypeError, StopIteration):
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print("Controlled AI protocol fixture: http://127.0.0.1:18808/v1 (loopback only)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 18808), Handler).serve_forever()
