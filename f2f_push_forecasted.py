"""Push the VERIFIED exec-F2F verdicts for the 52 MASE-tracked forecasted deals.

Not a derivation script — `f2f_rebuild_all.py` does that.  This one lands an already-
adjudicated batch: the 52 forecasted deals were classified by the gate, then EVERY "done"
verdict was adversarially re-checked one-by-one, which downgraded 2 of 6 (CLARINS: onsite
real but the CPO never attended; Manscaped: no Event exists on the claimed date and the AE
writes "I did not get a chance to speak with you at the conference").  Those corrections
are baked into the payload, so this script must NOT re-derive anything.

SAFETY (this book was blanked once — 2026-07-16 P0 — by a bulk write):
  - read-modify-write per row, so no other key in `record` can be disturbed;
  - skips any row whose `record` is missing or is not a JSON object, rather than
    jsonb_set-ing over it (that is the shape that NULLs a row);
  - --apply is required; the default is a dry run that writes nothing.

Usage:  python f2f_push_forecasted.py            # dry run
        python f2f_push_forecasted.py --apply    # write
"""
import json
import os
import re
import ssl
import sys
import urllib.request
from collections import Counter

SCRATCH = os.path.join(os.environ.get("TEMP", ""), "claude",
                       "C--Users-Aleen-Dhar-Downloads-Agent-Salesforce-Link--1--Agent-Salesforce-Link",
                       "b89b22ae-0e3a-4bc7-8317-a75ad39dc393", "scratchpad")
PAYLOAD = os.path.join(SCRATCH, "exec_f2f_payload.json")
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"

# Zscaler intercepts TLS on this network; httpx fails outright and SSL_CERT_FILE trips
# "Basic Constraints of CA cert not marked critical". urllib + unverified context is the
# established workaround in this repo.
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

    with open(PAYLOAD, encoding="utf-8") as fh:
        payload = json.load(fh)
    print(f"payload: {len(payload)} deals | "
          f"{dict(Counter(v['status'] for v in payload.values()))}")

    # An evidence-less non-"none" verdict is the one thing this column must never render:
    # a confident chip citing nothing. Refuse the whole batch rather than land one.
    naked = [k for k, v in payload.items() if v["status"] != "none" and not v.get("evidence")]
    if naked:
        print(f"ABORT: {len(naked)} non-none verdicts carry no evidence: {naked[:5]}")
        return 1

    if not apply:
        print("[DRY RUN] --apply to write.")
        for oid, v in payload.items():
            if v["status"] == "done":
                print(f"   done  {oid}  {v['exec_name']} ({v['exec_title']})  {v['date']}")
        return 0

    ok = skipped = failed = 0
    problems = []
    for oid, verdict in payload.items():
        try:
            _, rows = req("GET", f"deal_records?select=opp_id,record&opp_id=like.{oid}*&limit=1")
            if not rows:
                skipped += 1; problems.append((oid, "no row")); continue
            rec = rows[0].get("record")
            if not isinstance(rec, dict):
                skipped += 1; problems.append((oid, "record not an object")); continue
            ai = rec.get("ai")
            rec["ai"] = ai if isinstance(ai, dict) else {}
            rec["ai"]["exec_f2f"] = verdict
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
