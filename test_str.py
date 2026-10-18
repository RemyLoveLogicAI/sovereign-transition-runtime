#!/usr/bin/env python3
"""STR v0.1 acceptance test — the exact two-client fleet approval experiment.

Clients: A = terminal (curl), B = HTTP client (python urllib). Both submit the
identical approval contract; only request_id differs. The runtime must:
  execute once, reject stale, replay duplicates with the same receipt, and
  fail closed on signature/scope drift.
Exit 0 = all green.
"""
import hashlib, hmac, json, os, sqlite3, subprocess, sys, time, urllib.request, uuid

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
DB = "state.db"
SECRET = "str-dev-sovereign-key"

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  -> {detail}" if not cond and detail else ""))

def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"))
def sign(p): return hmac.new(SECRET.encode(), canon(p).encode(), hashlib.sha256).hexdigest()

def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = json.dumps(body).encode() if body is not None else None
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def client_signature(pid, exp, action, rid):
    return sign({"proposal_id": pid, "expected_version": exp,
                 "action": action, "request_id": rid})

def curl_approve(pid, exp, action, rid):  # Client A: terminal transport
    sig = client_signature(pid, exp, action, rid)
    out = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-X", "POST", BASE + "/approve",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"proposal_id": pid, "expected_version": exp,
                           "action": action, "request_id": rid,
                           "client_signature": sig})],
        capture_output=True, text=True)
    body, code = out.stdout.strip().rsplit("\n", 1)
    return int(code), json.loads(body)

def receipts_count():
    return sqlite3.connect(DB).execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

def main():
    if os.path.exists(DB): os.remove(DB)
    for s in ("-wal", "-shm"):
        if os.path.exists(DB + s): os.remove(DB + s)
    daemon = subprocess.Popen([sys.executable, "strd.py"],
                              env={**os.environ, "STR_PORT": str(PORT), "STR_DB": DB,
                                   "STR_SECRET": SECRET})
    time.sleep(0.8)
    try:
        # Seed: task blocked, state at version 42 (per experiment spec)
        code, r = http("POST", "/seed", {"tasks": [{"task_id": "FLEET_SYNC", "status": "blocked"}],
                                         "state_version": 42})
        check("seed ok", code == 200 and r["state_version"] == 42, str(r))

        # 1. Agent authors proposal against version 42
        code, prop = http("POST", "/proposals",
                          {"task_id": "FLEET_SYNC", "action": "APPROVE_FLEET_SYNC",
                           "diff": {"set": {"status": "in_sync"}}})
        pid = prop.get("proposal_id", "")
        check("proposal created @ base_version 42", code == 201 and prop.get("base_version") == 42, str(prop))

        # 2. Render proposal to BOTH clients (identical action payload contract)
        rid_a, rid_b = uuid.uuid4().hex, uuid.uuid4().hex  # distinct idempotency keys
        sig_a = client_signature(pid, 42, "APPROVE_FLEET_SYNC", rid_a)
        sig_b = client_signature(pid, 42, "APPROVE_FLEET_SYNC", rid_b)
        check("rendered: identical contract, distinct request ids",
              sig_a != sig_b and pid.startswith("prop_"))

        # 3. Client A (terminal) approves -> version 43, receipt
        code, r = curl_approve(pid, 42, "APPROVE_FLEET_SYNC", rid_a)
        check("A approves: executes once -> v43 receipt",
              code == 200 and r["receipt"]["new_version"] == 43 and not r["idempotent"], str(r))
        receipt_a = r["receipt"]

        # 4. Client B approves same proposal with stale expected_version 42
        code, r = http("POST", "/approve",
                       {"proposal_id": pid, "expected_version": 42,
                        "action": "APPROVE_FLEET_SYNC", "request_id": rid_b,
                        "client_signature": sig_b})
        check("B stale approval -> ERR_STALE_VERSION (expected 42, current 43)",
              code == 409 and r["error"] == "ERR_STALE_VERSION"
              and r["expected"] == 42 and r["current"] == 43, str(r))

        # 5. A retries same request (disconnect/retry) -> same receipt, zero side effects
        before = receipts_count()
        code, r = curl_approve(pid, 42, "APPROVE_FLEET_SYNC", rid_a)
        after = receipts_count()
        check("A retry -> idempotent receipt replay, zero new side effects",
              code == 200 and r["idempotent"] and r["receipt"]["ledger_seq"] == receipt_a["ledger_seq"]
              and before == after, str(r))

        # 6. State advanced: task is in_sync, exactly one transition
        code, r = http("GET", "/state")
        task = [t for t in r["fleet_tasks"] if t["task_id"] == "FLEET_SYNC"][0]
        check("task transitioned exactly once (blocked -> in_sync, version 43)",
              r["state_version"] == 43 and task["status"] == "in_sync"
              and len(r["receipts"]) == 1, str(r["receipts"]))

        # 7. Scope drift: wrong action for proposal -> ERR_ACTION_MISMATCH
        rid7 = uuid.uuid4().hex
        code, r = http("POST", "/approve",
                       {"proposal_id": pid, "expected_version": 43,
                        "action": "DELETE_EVERYTHING", "request_id": rid7,
                        "client_signature": client_signature(pid, 43, "DELETE_EVERYTHING", rid7)})
        check("scope drift rejected -> ERR_ACTION_MISMATCH",
              code == 403 and r["error"] == "ERR_ACTION_MISMATCH", str(r))

        # 8. Forgery: bad signature -> ERR_SIGNATURE
        code, r = http("POST", "/approve",
                       {"proposal_id": pid, "expected_version": 43,
                        "action": "APPROVE_FLEET_SYNC", "request_id": uuid.uuid4().hex,
                        "client_signature": "deadbeef"})
        check("bad signature -> ERR_SIGNATURE", code == 401 and r["error"] == "ERR_SIGNATURE", str(r))

        # 9. Unknown proposal -> ERR_UNKNOWN_PROPOSAL
        rid9 = uuid.uuid4().hex
        code, r = http("POST", "/approve",
                       {"proposal_id": "prop_nope", "expected_version": 43,
                        "action": "APPROVE_FLEET_SYNC", "request_id": rid9,
                        "client_signature": client_signature("prop_nope", 43, "APPROVE_FLEET_SYNC", rid9)})
        check("unknown proposal -> ERR_UNKNOWN_PROPOSAL",
              code == 404 and r["error"] == "ERR_UNKNOWN_PROPOSAL", str(r))

        # 10. Out-of-scope diff field rejected at authoring... (execution-time guard)
        code, prop2 = http("POST", "/proposals",
                           {"task_id": "FLEET_SYNC", "action": "REBOOT_MINI",
                            "diff": {"set": {"shell": "rm -rf /"}}})
        check("dangerous proposal can be authored (agent proposes, runtime judges)",
              code == 201)
        rid10 = uuid.uuid4().hex
        code, r = http("POST", "/approve",
                       {"proposal_id": prop2["proposal_id"], "expected_version": 43,
                        "action": "REBOOT_MINI", "request_id": rid10,
                        "client_signature": client_signature(prop2["proposal_id"], 43, "REBOOT_MINI", rid10)})
        check("out-of-scope field execution blocked -> ERR_FIELD_OUT_OF_SCOPE",
              code == 403 and r["error"] == "ERR_FIELD_OUT_OF_SCOPE", str(r))

        # 11. Hash-chained receipts: second transition, chain verifies
        _, _ = http("POST", "/seed", {"tasks": [{"task_id": "BOOT_MINI", "status": "off"}]})
        code, prop3 = http("POST", "/proposals",
                            {"task_id": "BOOT_MINI", "action": "BOOT_MINI",
                             "diff": {"set": {"status": "on"}}})
        rid11 = uuid.uuid4().hex
        code, r = http("POST", "/approve",
                       {"proposal_id": prop3["proposal_id"], "expected_version": 43,
                        "action": "BOOT_MINI", "request_id": rid11,
                        "client_signature": client_signature(prop3["proposal_id"], 43, "BOOT_MINI", rid11)})
        check("second transition executes -> v44 receipt", code == 200 and r["receipt"]["new_version"] == 44, str(r))
        code, v = http("GET", "/verify")
        check("ledger chain verifies (2 links, tamper-free)",
              code == 200 and v["ok"] and v["receipts"] == 2 and v["root"], str(v))

        # 12. Tamper detection: forge a receipt field, chain must break at that link
        tc = sqlite3.connect(DB)
        tc.execute("UPDATE receipts SET result=? WHERE ledger_seq=1", (json.dumps({"forged": True}),))
        tc.commit(); tc.close()
        code, v = http("GET", "/verify")
        check("tampered receipt detected -> chain broken at seq 1",
              code == 200 and not v["ok"] and v["broken_at_seq"] == 1, str(v))

        print("\n" + "=" * 58)
        print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
        for n, d in FAIL: print(f"  FAILED: {n} -> {d}")
        print("=" * 58)
        return 1 if FAIL else 0
    finally:
        daemon.terminate()

if __name__ == "__main__":
    sys.exit(main())
