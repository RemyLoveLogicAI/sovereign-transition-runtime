#!/usr/bin/env python3
"""Sovereign Transition Runtime (STR) v0.1 prototype.

Core primitive: agents never mutate state directly. Agents author proposals
against a base state version; users submit narrowly-scoped approvals from any
client. The runtime executes exactly once, fails closed on stale/duplicate/
unauthorized, and emits an append-only receipt per accepted transition.

Semantics (fail-closed, in check order):
  1. signature over (proposal_id, expected_version, action, request_id)
  2. proposal exists and is open-or-executed
  3. action matches proposal action (scope binding)
  4. request_id replay -> return existing receipt, zero side effects
  5. OCC: expected_version == current state_version, else ERR_STALE_VERSION
  6. execute in one transaction: apply permitted diff, bump version, receipt

Run: STR_PORT=8797 python3 strd.py
"""
import json, hashlib, hmac, os, sqlite3, time, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = os.environ.get("STR_DB", "state.db")
SECRET = os.environ.get("STR_SECRET", "str-dev-sovereign-key")
PORT = int(os.environ.get("STR_PORT", "8797"))

def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"))

def sign(payload):
    return hmac.new(SECRET.encode(), canon(payload).encode(), hashlib.sha256).hexdigest()

def db():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
    INSERT OR IGNORE INTO kv VALUES ('state_version', 0);
    CREATE TABLE IF NOT EXISTS fleet_tasks (
      task_id TEXT PRIMARY KEY, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS proposals (
      proposal_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
      action TEXT NOT NULL, base_version INTEGER NOT NULL,
      diff TEXT NOT NULL, diff_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS receipts (
      ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
      proposal_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
      new_version INTEGER NOT NULL, result TEXT NOT NULL, executed_at REAL NOT NULL);
    """)
    c.close()

def version(c):
    return c.execute("SELECT v FROM kv WHERE k='state_version'").fetchone()[0]

def create_proposal(body):
    task_id, action, diff = body["task_id"], body["action"], body["diff"]
    c = db()
    task = c.execute("SELECT status FROM fleet_tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        c.close(); return 404, {"error": "ERR_TASK_MISSING"}
    v = version(c)
    diff_hash = hashlib.sha256(canon(diff).encode()).hexdigest()
    pid = "prop_" + uuid.uuid4().hex[:10]
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)",
              (pid, task_id, action, v, json.dumps(diff), diff_hash, "open", time.time()))
    c.execute("COMMIT"); c.close()
    return 201, {"proposal_id": pid, "base_version": v, "diff_hash": diff_hash,
                 "action": action, "task_id": task_id}

def approve(body):
    pid = body["proposal_id"]; exp = body["expected_version"]
    action = body["action"]; rid = body["request_id"]
    if sign({"proposal_id": pid, "expected_version": exp, "action": action,
             "request_id": rid}) != body["client_signature"]:
        return 401, {"error": "ERR_SIGNATURE"}
    c = db()
    p = c.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
    if p is None:
        c.close(); return 404, {"error": "ERR_UNKNOWN_PROPOSAL"}
    if p["action"] != action:
        c.close(); return 403, {"error": "ERR_ACTION_MISMATCH",
                                "proposal_action": p["action"]}
    replay = c.execute("SELECT * FROM receipts WHERE request_id=?", (rid,)).fetchone()
    if replay is not None:  # idempotent replay: receipt, zero side effects
        out = {"receipt": dict(replay), "idempotent": True}
        c.close(); return 200, out
    cur = version(c)
    if exp != cur:
        c.close(); return 409, {"error": "ERR_STALE_VERSION",
                                "expected": exp, "current": cur}
    if p["status"] != "open":
        c.close(); return 409, {"error": "ERR_PROPOSAL_NOT_OPEN",
                                "status": p["status"]}
    # execute exactly once, atomically
    c.execute("BEGIN IMMEDIATE")
    diff = json.loads(p["diff"])
    sets = diff.get("set", {})
    for field, value in sets.items():
        if field not in ("status",):
            c.execute("ROLLBACK"); c.close()
            return 403, {"error": "ERR_FIELD_OUT_OF_SCOPE", "field": field}
        c.execute("UPDATE fleet_tasks SET status=? WHERE task_id=?", (value, p["task_id"]))
    new_v = cur + 1
    c.execute("UPDATE kv SET v=? WHERE k='state_version'", (new_v,))
    c.execute("UPDATE proposals SET status='executed' WHERE proposal_id=?", (pid,))
    result = {"task_id": p["task_id"], "action": p["action"], "applied": sets}
    c.execute("INSERT INTO receipts (proposal_id, request_id, new_version, result, executed_at)"
              " VALUES (?,?,?,?,?)", (pid, rid, new_v, json.dumps(result), time.time()))
    receipt = dict(c.execute("SELECT * FROM receipts WHERE request_id=?", (rid,)).fetchone())
    c.execute("COMMIT"); c.close()
    return 200, {"receipt": receipt, "idempotent": False}

def seed(body):
    c = db()
    c.execute("BEGIN IMMEDIATE")
    for t in body.get("tasks", []):
        c.execute("INSERT OR REPLACE INTO fleet_tasks VALUES (?,?)",
                  (t["task_id"], t["status"]))
    if "state_version" in body:
        c.execute("UPDATE kv SET v=? WHERE k='state_version'", (body["state_version"],))
    out = {"state_version": version(c)}
    c.execute("COMMIT"); c.close()
    return 200, out

def state():
    c = db()
    tasks = [dict(r) for r in c.execute("SELECT * FROM fleet_tasks")]
    ledger = [dict(r) for r in c.execute(
        "SELECT ledger_seq, proposal_id, new_version, result, executed_at FROM receipts"
        " ORDER BY ledger_seq")]
    out = {"state_version": version(c), "fleet_tasks": tasks, "receipts": ledger}
    c.close(); return 200, out

class H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/proposals": r = create_proposal(body)
        elif self.path == "/approve": r = approve(body)
        elif self.path == "/seed": r = seed(body)
        else: return self._send(404, {"error": "not found"})
        self._send(*r)
    def do_GET(self):
        if self.path == "/state": return self._send(*state())
        self._send(404, {"error": "not found"})

if __name__ == "__main__":
    init()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
