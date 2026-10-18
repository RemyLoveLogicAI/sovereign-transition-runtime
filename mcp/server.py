#!/usr/bin/env python3
"""STR MCP wrapper — exposes the proposal surface to agent clients (MCP Router).

Agents PROPOSE through MCP. They never approve. Approval belongs to the
sovereign user via terminal or signed bearer link, hitting STR's /approve
directly. This is the trust boundary by design.

Tools exposed (exactly three — deliberately narrow):
  propose_transition(task_id, target_status, payload)  -> proposal_id, base_version, hash
  get_pending_proposals()                              -> open proposals
  get_receipt(receipt_id)                              -> one ledger receipt

Wire into Mac Mini MCP Router (port 3282): add this as a local stdio server.
Env: STR_PORT (default 8797), STR_SECRET (must match the daemon's key).

Protocol: JSON-RPC 2.0 over stdio (MCP initialize / tools/list / tools/call).
"""
import json, os, sys, urllib.request

PORT = int(os.environ.get("STR_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"

TOOLS = [
    {
        "name": "propose_transition",
        "description": "Propose a state transition for a task. Agents propose; "
                       "only the user can approve. Returns proposal_id, the "
                       "base_version it was authored against, and the diff hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "target_status": {"type": "string"},
                "payload": {"type": "object", "default": {}},
            },
            "required": ["task_id", "target_status"],
        },
    },
    {
        "name": "get_pending_proposals",
        "description": "List open (unapproved) proposals with their base "
                       "versions and diff hashes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_receipt",
        "description": "Fetch a receipt by its ledger seq, proposal_id, or "
                       "request_id. Receipts are hash-chained and append-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"receipt_id": {"type": "string"}},
            "required": ["receipt_id"],
        },
    },
]


def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {"error": f"str_unreachable: {e}"}


def call_tool(name, args):
    if name == "propose_transition":
        diff = {"set": {"status": args["target_status"]}}
        for k, v in (args.get("payload") or {}).items():
            diff["set"][k] = v
        return http("POST", "/proposals",
                    {"task_id": args["task_id"],
                     "action": f"SET_{args['target_status'].upper()}",
                     "diff": diff})
    if name == "get_pending_proposals":
        state = http("GET", "/state")
        return {"pending": [p for p in state.get("proposals", [])
                            if p.get("status") == "open"]}
    if name == "get_receipt":
        state = http("GET", "/state")
        key = args["receipt_id"]
        for r in state.get("receipts", []):
            if str(r["ledger_seq"]) == key or r["proposal_id"] == key:
                return r
        return {"error": "ERR_RECEIPT_NOT_FOUND"}
    return {"error": f"unknown tool {name}"}


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        method, rid = msg.get("method"), msg.get("id")
        result = None
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "str", "version": "0.3.0"}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = call_tool(msg["params"]["name"], msg["params"].get("arguments", {}))
        if result is not None and rid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "result": result}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
