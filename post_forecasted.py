"""Postprocess + upsert the 59 freshly-swept forecasted opps, then summarize:
win scores, scope-shrink detections, CEO watches gained, reason quality."""
import json, os, sys
import cc_sweep
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ids = [i[:15] for i in json.load(open("cc_work/_pending.json"))]
res = cc_sweep.postprocess_from_files(ids, upsert=True)
print(f"\n=== POSTPROCESS DONE: {len(res)} upserted ===")

shrinks, ceo_needed, ceo_scope, has_evidence = [], [], [], 0
for oid in ids:
    fp = os.path.join("cc_work", oid + ".final.json")
    if not os.path.exists(fp):
        continue
    try:
        rec = json.load(open(fp, encoding="utf-8"))
    except Exception:
        continue
    ai = rec.get("ai") or {}
    hard = rec.get("hard") or {}
    acct = (hard.get("account_name") or oid)[:26]
    sc = ai.get("scope_change") or {}
    if str(sc.get("direction") or "").lower() in ("reduced", "reduced_scope", "shrunk", "narrowed"):
        shrinks.append(acct)
    ci = ai.get("ceo_intervention") or {}
    if ci.get("needed"):
        ceo_needed.append(acct)
    if any(r.get("type") == "scope_shrink" for r in (ci.get("reasons") or [])):
        ceo_scope.append(acct)
    if (ai.get("deal_scores_evidence") or {}).get("ai_reasons") or (ai.get("deal_scores_evidence") or {}).get("summary"):
        has_evidence += 1

print(f"scope reduced: {len(shrinks)} -> {shrinks}")
print(f"CEO needed (any watch/support): {len(ceo_needed)} -> {ceo_needed}")
print(f"CEO scope_shrink watch: {len(ceo_scope)} -> {ceo_scope}")
print(f"deals carrying deal_scores_evidence (narrative reasons): {has_evidence}/{len(ids)}")
