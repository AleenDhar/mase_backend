"""Local verification of the CEO + vendor-dedup fixes (no network)."""
import json, re, datetime
import deal_engine_packets as P
import deal_engine_ceo as CEO

# ---- vendor dedup: build index from the local dict, run on the real cloud CC record ----
vd = json.load(open("engines_dump/vendordict_v1.0.txt", encoding="utf-8"))
idx = []
for v in vd.get("vendors", []):
    canon = (v.get("canonical") or "").strip()
    if not canon:
        continue
    for nm in [canon] + list(v.get("aliases") or []):
        tok = re.sub(r"[^a-z0-9]", "", str(nm).lower())
        if tok:
            idx.append((tok, canon, len(tok)))
idx.sort(key=lambda x: x[2], reverse=True)

rec = json.load(open("dryrun_forecasted/006P700000OcxpH.json", encoding="utf-8"))
comps = ((rec.get("ai") or {}).get("competitive_position") or {}).get("competitors") or []
print("=== VENDOR DEDUP (Consumer Cellular cloud record) ===")
print(f"BEFORE ({len(comps)}):", [c.get("name") for c in comps])
ded = P._dedupe_competitors(comps, index=idx)
print(f"AFTER  ({len(ded)}):", [c.get("name") for c in ded])

# ---- CEO fixes: summary populated + >90d watch dropped ----
print("\n=== CEO FIXES ===")
today = datetime.date.today()
old = (today - datetime.timedelta(days=140)).isoformat()
recent = (today - datetime.timedelta(days=10)).isoformat()
parsed = {"ai": {"deal_scores": {"headline": {"win_position": 55, "deal_momentum": 30}},
                 "ceo_intervention": {"support": {"needed": True, "areas": ["exec_connect"],
                     "reason": "Economic buyer never engaged; CEO must open the exec relationship.",
                     "ceo_action": "CEO connects to the economic buyer to unblock.", "priority": "high"}}},
          "hard": {"amount": 500000}}
prior_ai = {"ceo_intervention": {"reasons": [
    {"type": "large_slowdown", "act": False, "as_of": old, "severity": "high",
     "summary": "OLD watch >90d — should be DROPPED"},
    {"type": "competitor_edge", "act": False, "as_of": recent, "severity": "medium",
     "summary": "RECENT watch <90d — should be KEPT"}]}}
CEO.finalize_ceo_intervention(parsed, {"forecast_category": "Commit", "amount": 500000},
                              {"contacts": []}, prior_ai=prior_ai)
ci = parsed["ai"]["ceo_intervention"]
print("summary   :", repr(ci.get("summary")))
print("needed    :", ci.get("needed"))
print("reasons   :", [(r.get("type"), r.get("as_of")) for r in ci.get("reasons", [])])
print("OLD dropped?:", not any("OLD" in str(r.get("summary", "")) for r in ci.get("reasons", [])))
print("RECENT kept?:", any("RECENT" in str(r.get("summary", "")) for r in ci.get("reasons", [])))
print("summary non-empty?:", bool(ci.get("summary")))
