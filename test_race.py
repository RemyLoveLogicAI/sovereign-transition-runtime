#!/usr/bin/env python3
"""STR race proof: N concurrent approvals of one proposal -> exactly one execution.

Fires 4 approvals simultaneously from 4 threads (4 distinct clients).
Pass criteria: exactly one 200 receipt, three ERR_STALE_VERSION, one ledger
row, state advanced exactly once. Exit 0 = EXACTLY-ONCE.
"""
import hashlib, hmac, json, os, sqlite3, subprocess, sys, time, urllib.request, uuid, threading

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
DB = "race.db"
SECRET = "str-dev-sovereign-key"
N_CLIENTS = 4

canon = lambda o: json.dumps(o, sort_keys=True, separators=(",", ":"))
sign = lambda p: hmac.new(SECRET.encode(), canon(p).encode(), hashlib.sha256).hexdigest()

def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def main():
    for f in (DB, DB + "-wal", DB + "-shm"):
        if os.path.exists(f): os.remove(f)
    d = subprocess.Popen([sys.executable, "strd.py"],
                         env={**os.environ, "STR_PORT": str(PORT), "STR_DB": DB, "STR_SECRET": SECRET})
    time.sleep(0.8)
    try:
        http("POST", "/seed", {"tasks": [{"task_id": "FLEET_SYNC", "status": "blocked"}],
                               "state_version": 42})
        _, prop = http("POST", "/proposals",
                       {"task_id": "FLEET_SYNC", "action": "APPROVE_FLEET_SYNC",
                        "diff": {"set": {"status": "in_sync"}}})
        pid = prop["proposal_id"]
        results, lock = [], threading.Lock()

        def fire(tag):
            rid = uuid.uuid4().hex
            payload = {"proposal_id": pid, "expected_version": 42,
                       "action": "APPROVE_FLEET_SYNC", "request_id": rid}
            payload["client_signature"] = sign(payload)
            code, r = http("POST", "/approve", payload)
            with lock:
                results.append((tag, code, r.get("error") or r.get("receipt", {}).get("new_version")))

        ts = [threading.Thread(target=fire, args=(t,)) for t in "ABCD"]
        [t.start() for t in ts]; [t.join() for t in ts]
        code, st = http("GET", "/state")
        n = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        ok = (st["state_version"] == 43 and n == 1 and st["fleet_tasks"][0]["status"] == "in_sync"
              and sum(1 for x in results if x[1] == 200) == 1
              and sum(1 for x in results if x[2] == "ERR_STALE_VERSION") == N_CLIENTS - 1)
        print("concurrent:", results)
        print(f"state_version={st['state_version']} receipts={n} task={st['fleet_tasks'][0]['status']}")
        print("RACE VERDICT:", "EXACTLY-ONCE" if ok else "BROKEN")
        return 0 if ok else 1
    finally:
        d.terminate()
        for f in (DB, DB + "-wal", DB + "-shm"):
            if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    sys.exit(main())
