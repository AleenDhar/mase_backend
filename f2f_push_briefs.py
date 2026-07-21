"""Attach the meeting-substance BRIEF to each forecasted deal's exec_f2f verdict.

The brief is a 1-2 sentence summary of what is actually happening on the deal — what was
discussed recently and the one major update/decision/blocker — summarized from the SFDC
Next Step log (the rep's own dated notes). It is the content shown on the Executive Connect
column tooltip, replacing the metadata (date/channel) the reader did not want.

Stored at record.ai.exec_f2f.brief so it rides the existing slim_record + carry-forward
plumbing to the browser with no new field. The sweep preserves it across recomputes
(deal_engine_sweep.py, exec_f2f block).

Read-modify-write per row; skips any row whose record is missing/not an object. --apply required.
"""
import json
import os
import re
import ssl
import sys
import urllib.request

SCRATCH = os.path.join(os.environ.get("TEMP", ""), "claude",
                       "C--Users-Aleen-Dhar-Downloads-Agent-Salesforce-Link--1--Agent-Salesforce-Link",
                       "b89b22ae-0e3a-4bc7-8317-a75ad39dc393", "scratchpad")
BRIEFS = os.path.join(SCRATCH, "f2f_briefs.json")
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
CTX = ssl._create_unverified_context()


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^(\w+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2)
    return env


def main():
    apply = "--apply" in sys.argv
    env = load_env(ENV)
    url = env["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
    key = env["SUPABASE_SERVICE_ROLE_KEY"]

    def req(method, path, body=None, extra=None):
        headers = {"apikey": key, "Authorization": "Bearer " + key,
                   "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"{url}/rest/v1/{path}", data=data,
                                   headers=headers, method=method)
        resp = urllib.request.urlopen(r, context=CTX, timeout=60)
        txt = resp.read().decode()
        return resp.status, (json.loads(txt) if txt.strip() else None)

    with open(BRIEFS, encoding="utf-8") as fh:
        briefs = json.load(fh)
    print(f"briefs: {len(briefs)}")
    if not apply:
        print("[DRY RUN] --apply to write.")
        return 0

    ok = skipped = failed = 0
    problems = []
    for oid, brief in briefs.items():
        try:
            _, rows = req("GET", f"deal_records?select=opp_id,record&opp_id=like.{oid}*&limit=1")
            if not rows:
                skipped += 1; problems.append((oid, "no row")); continue
            rec = rows[0].get("record")
            if not isinstance(rec, dict):
                skipped += 1; problems.append((oid, "record not an object")); continue
            ai = rec.get("ai")
            rec["ai"] = ai if isinstance(ai, dict) else {}
            ef = rec["ai"].get("exec_f2f")
            rec["ai"]["exec_f2f"] = ef if isinstance(ef, dict) else {}
            rec["ai"]["exec_f2f"]["brief"] = brief
            status, _ = req("PATCH", f"deal_records?opp_id=like.{oid}*",
                            {"record": rec}, {"Prefer": "return=minimal"})
            if status in (200, 204):
                ok += 1
            else:
                failed += 1; problems.append((oid, f"PATCH {status}"))
        except Exception as exc:  # noqa: BLE001
            failed += 1; problems.append((oid, str(exc)[:70]))

    print(f"wrote ok={ok} skipped={skipped} failed={failed}")
    for p in problems[:10]:
        print("   ", p)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
