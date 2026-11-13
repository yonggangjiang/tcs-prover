"""TCS Prover's MCP server "tcs": a thin stdio bridge to the service of the runner that started this CLI.

The author's (or coordinator's) CLI starts this program as an MCP server over stdio (newline-delimited JSON-RPC).
It answers initialize and ping itself and forwards tools/list and tools/call, one connection each, to the runner's
service (runner/team.py Service) at the address of TCS_MCP_ADDRESS, with the token of TCS_MCP_TOKEN; both come from
the CLI's settings for this server, never from argv. Calls run concurrently (a team_wait may wait up to 30 minutes
while other tools answer). Standard library only: it runs with `python -I`, outside the checkout's import path.
"""

import json
import os
import socket
import sys
import threading

ADDRESS_ENV = "TCS_MCP_ADDRESS"
TOKEN_ENV = "TCS_MCP_TOKEN"
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
MESSAGE_LIMIT = 16 * 1024 * 1024
INSTRUCTIONS = "TCS Prover's tools for this run: the goal tool and, for a coordinator, its team."

_output_lock = threading.Lock()


def write(message):
    with _output_lock:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def forward(method, params):
    """Send one request to the runner's service; return its result, or raise OSError/ValueError."""

    address = os.environ.get(ADDRESS_ENV, "")
    host, _, port = address.rpartition(":")
    if host != "127.0.0.1" or not port.isdigit():
        raise ValueError("TCS Prover's service address is missing.")
    with socket.create_connection((host, int(port))) as connection:
        stream = connection.makefile("rwb")
        stream.write(json.dumps({"token": os.environ.get(TOKEN_ENV, ""), "method": method, "params": params},
                                ensure_ascii=False).encode("utf-8") + b"\n")
        stream.flush()
        line = stream.readline(MESSAGE_LIMIT + 1)
    if not line.endswith(b"\n"):
        raise ValueError("TCS Prover's service gave no answer (the run may have stopped).")
    answer = json.loads(line.decode("utf-8"))
    if not isinstance(answer, dict) or "result" not in answer:
        raise ValueError(str((answer or {}).get("error") or "TCS Prover's service refused the request."))
    return answer["result"]


def answer_call(request_id, params):
    try:
        result = forward("tools/call", params)
    except (OSError, ValueError, UnicodeError) as exc:
        result = {"content": [{"type": "text", "text": f"TCS Prover's service cannot be reached: {exc}"}],
                  "isError": True}
    write({"jsonrpc": "2.0", "id": request_id, "result": result})


def handle(message):
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        if isinstance(message, dict) and "id" in message:
            write({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32600, "message": "Invalid request."}})
        return
    if "id" not in message:
        return  # A notification (initialized, cancelled): nothing to answer.
    request_id, method = message["id"], message["method"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        write({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": requested if requested in VERSIONS else VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tcs", "version": "1.0.0"}, "instructions": INSTRUCTIONS}})
    elif method == "ping":
        write({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "tools/list":
        try:
            result = forward("tools/list", {})
        except (OSError, ValueError, UnicodeError) as exc:
            write({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32000, "message": f"TCS Prover's service cannot be reached: {exc}"}})
            return
        write({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "tools/call":
        threading.Thread(target=answer_call, args=(request_id, params), daemon=True).start()
    else:
        write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found."}})


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}})
            continue
        handle(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
