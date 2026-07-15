#!/usr/bin/env python
"""Minimal MCP stdio E2E client for fmdw_mcp_server.py (no dependencies).

Speaks raw newline-delimited JSON-RPC: initialize -> initialized ->
tools/list -> tools/call. Each invocation spawns a fresh server process,
which also proves that job metadata persists across server restarts.

Usage:
    python mcp_e2e_client.py list
    python mcp_e2e_client.py start <input_pdf> [domain] [vision_check]
    python mcp_e2e_client.py status <job_id>
    python mcp_e2e_client.py report <job_id> [--head N]
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SERVER = [str(REPO / ".venv/bin/python"), str(REPO / "fmdw_mcp_server.py")]


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            SERVER, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(REPO),
        )
        self._id = 0

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method,
                    "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"RPC error: {msg['error']}")
                return msg["result"]

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def handshake(self):
        res = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fmdw-e2e", "version": "0.1"},
        })
        self.notify("notifications/initialized")
        return res

    def call_tool(self, name, arguments):
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            raise RuntimeError(f"tool error: {json.dumps(res, ensure_ascii=False)[:800]}")
        sc = res.get("structuredContent")
        if sc is not None:
            return sc
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return {"_raw": joined}

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    c = Client()
    try:
        info = c.handshake()
        server_name = info.get("serverInfo", {}).get("name")
        if cmd == "list":
            tools = c.request("tools/list")["tools"]
            print(f"HANDSHAKE-OK server={server_name} protocol={info.get('protocolVersion')}")
            for t in tools:
                print(f"TOOL {t['name']}: {t.get('description', '').splitlines()[0]}")
        elif cmd == "start":
            args = {"input_pdf": sys.argv[2]}
            if len(sys.argv) > 3:
                args["domain"] = sys.argv[3]
            if len(sys.argv) > 4:
                args["vision_check"] = sys.argv[4]
            out = c.call_tool("start_convert", args)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif cmd == "status":
            out = c.call_tool("job_status", {"job_id": sys.argv[2]})
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif cmd == "report":
            out = c.call_tool("get_qa_report", {"job_id": sys.argv[2]})
            report = out.pop("report", None)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            if report:
                head = 60
                if "--head" in sys.argv:
                    head = int(sys.argv[sys.argv.index("--head") + 1])
                print("----- goose_qa_report.md (head) -----")
                print("\n".join(report.splitlines()[:head]))
                print(f"----- (total {len(report.splitlines())} lines) -----")
        else:
            print(__doc__)
            return 2
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
