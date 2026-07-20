"""Read-only: does the LOCKED win/mom engine text cover the CRO's points?

Points to check:
  A. RFP / formal-evaluation quiet window — don't penalize meeting gaps when the buyer is
     running a structured evaluation and still engaging.
  B. Buyer-INITIATED non-meeting milestones (issuing RFP round 2 docs, deadlines, scoring
     requests) count as engagement — not just Avoma meetings.
  C. Deal-strengthening trajectory: amount raised, forecast category up, stage advanced.
  D. Frequency factor / trajectory: after a burst of activity, is it strength-to-strength
     or a slowdown?
  E. No competitor confirmed + still shortlisted => not a losing position.
"""
import re, sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}


def vkey(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (-1,)


rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                    params={"select": "engine,version,content,locked", "locked": "is.true",
                            "engine": "in.(win,mom)"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
best = {}
for r in rows:
    e = r["engine"]
    if e not in best or vkey(r["version"]) > vkey(best[e]["version"]):
        best[e] = r

PROBES = {
    "A. RFP / evaluation quiet-window exception": [
        r"\bRFP\b", r"quiet period", r"evaluation window", r"blackout",
        r"procurement process", r"formal evaluation.*gap", r"tender"],
    "B. Buyer-initiated NON-meeting milestone counts as engagement": [
        r"buyer[- ]initiated", r"inbound", r"issued .*document", r"round 2", r"deadline",
        r"buyer action", r"procurement milestone"],
    "C. Deal-strengthening (amount / forecast / stage moved up)": [
        r"amount (grew|increase|raise)", r"forecast category", r"stage advanc",
        r"upgraded", r"deal strengthen"],
    "D. Frequency factor / trajectory (accelerating vs slowing)": [
        r"trajectory", r"frequenc", r"accelerat", r"decelerat", r"slow(ing|down)",
        r"strength to strength", r"trend"],
    "E. No competitor confirmed + shortlisted => not losing": [
        r"no competitor (named|confirmed)", r"sole[- ]source", r"shortlist"],
    "-- existing recency/decay machinery (for reference)": [
        r"recency", r"decay", r"days (since|dark)", r"\b30/60/90\b", r"stale"],
}

for eng in ("win", "mom"):
    row = best.get(eng)
    if not row:
        print(f"{eng}: NOT LOCKED"); continue
    txt = row["content"]
    print("=" * 96)
    print(f"ENGINE `{eng}`  LOCKED v{row['version']}   ({len(txt):,} chars)")
    print("=" * 96)
    for label, pats in PROBES.items():
        hits = []
        for p in pats:
            for m in re.finditer(p, txt, re.I):
                s = max(0, m.start() - 70)
                hits.append(txt[s:m.end() + 70].replace("\n", " ").strip())
        if hits:
            print(f"  [PRESENT] {label}")
            for h in hits[:2]:
                print(f"            …{h[:150]}…")
        else:
            print(f"  [ MISSING ] {label}")
    print()

# Section headings, so a proposed edit can name a real anchor.
for eng in ("win", "mom"):
    row = best.get(eng)
    if not row:
        continue
    print(f"--- `{eng}` v{row['version']} section headings ---")
    for line in row["content"].splitlines():
        if re.match(r"^\s*#{1,4}\s|^\s*§|^\s*\d+(\.\d+)*[a-z]?\s+[A-Z]", line):
            print("   ", line.strip()[:100])
    print()
