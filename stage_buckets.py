"""Bucket all ACTIVE opps into the staged resweep order the user asked for:
  S1  forecasted (any stage)                         [commit/best case/upside/upside key deal]
  S2  NOT forecasted AND stage >= Formal Evaluation  [incl. Formal Evaluation]
  S3  everything else remaining (qualified + below)
Writes cc_work/_stage{1,2,3}.json. Read-only. `--counts` just prints the tally."""
import json, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FC = {"commit", "best case", "upside", "upside key deal"}


def stage_rank(stage: str) -> int:
    s = (stage or "").strip().lower()
    if any(w in s for w in ("po received", "po-received", "signed", "closed won")): return 6
    if "contract" in s or "negotiat" in s: return 5
    if "vendor select" in s or s == "selected" or "6. contract" in s or "5. budget" in s: return 4
    if "shortlist" in s or "4. stakeholder" in s: return 3
    if "formal eval" in s or "evaluation" in s or "poc" in s or "3. evaluation" in s: return 2
    if "qualif" in s or "solution fitment" in s or "2. solution" in s: return 1
    return 0   # initial interest / generate interest / blank / unknown


def main():
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows, off = [], 0
    while True:
        b = requests.get(f"{base}/rest/v1/deal_records",
                         params={"select": "opp_id,forecast_category,stage,active", "active": "eq.true",
                                 "order": "opp_id.asc", "limit": 1000, "offset": off},
                         headers=h, verify=VERIFY, timeout=120).json()
        if not isinstance(b, list) or not b:
            break
        rows += b; off += len(b)
        if len(b) < 1000:
            break

    s1, s2, s3 = [], [], []
    from collections import Counter
    st_tally = Counter()
    for r in rows:
        oid = id15(r["opp_id"])
        fc = (r.get("forecast_category") or "").strip().lower()
        rank = stage_rank(r.get("stage"))
        st_tally[(r.get("stage") or "?")] += 1
        if fc in FC:
            s1.append(oid)
        elif rank >= 2:
            s2.append(oid)
        else:
            s3.append(oid)
    # dedup preserve order
    def dd(x):
        seen = set(); out = []
        for i in x:
            if i not in seen:
                seen.add(i); out.append(i)
        return out
    s1, s2, s3 = dd(s1), dd(s2), dd(s3)
    json.dump(s1, open("cc_work/_stage1.json", "w"))
    json.dump(s2, open("cc_work/_stage2.json", "w"))
    json.dump(s3, open("cc_work/_stage3.json", "w"))
    print(f"active opps: {len(rows)}")
    print(f"  S1 forecasted (any stage):                     {len(s1)}")
    print(f"  S2 non-forecasted, stage >= Formal Evaluation: {len(s2)}")
    print(f"  S3 the rest (qualified + below):               {len(s3)}")
    print(f"  total staged: {len(s1)+len(s2)+len(s3)}")
    print("\nstage distribution:")
    for st, n in st_tally.most_common():
        print(f"   {str(st)[:34]:34} {n}")


if __name__ == "__main__":
    main()
