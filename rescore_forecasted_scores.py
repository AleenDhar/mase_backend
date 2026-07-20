"""Re-score the FORECASTED book (forecast_critical = True) OFFLINE over their already-fresh
footprints — NO LLM sweep. The b833 named-batch sweep stamped OLD-code scores (legacy momentum
model, no qualification gate) on these deals before the fix deployed, so their momentum NUMBER
(legacy) no longer matches their engagement_v5 panel TEXT (SAMI: number 70/99 vs 'stalling' text).
Recomputing score + panel TOGETHER over the fresh footprints realigns them. Pins skipped. Flags any
deal whose momentum would drop hard (a stale-footprint drag we should NOT trust). Dry-run default."""
import sys, json, re
import requests, urllib3
import deal_engine_scoring as SC
import deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"; MTOK = sec["SUPABASE_ACCESS_TOKEN"]

rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,stage,forecast_critical,record", "active": "eq.true", "limit": "700"},
                    headers=H, verify=VERIFY, timeout=180).json()

def _has_footprints(ai):
    fp = ai.get("footprints") or {}
    eng = fp.get("engagement") or {}
    return bool(eng.get("points_60d") or eng.get("points_90d_process") or fp.get("buyer_touches_30d")
                or fp.get("meetings_60d") or fp.get("last_buyer_touch") or fp.get("last_meeting"))


out = {}; skip_pin = skip_noscore = skip_nofp = 0; movesW = []; movesM = []; needs_sweep = []
for r in rows:
    if not r.get("forecast_critical"):
        continue
    rec = r.get("record") or {}; ai = rec.get("ai") or {}
    if ai.get("pinned") or (ai.get("deal_scores") or {}).get("pinned"):
        skip_pin += 1; continue
    if not _has_footprints(ai):
        skip_nofp += 1; needs_sweep.append(str(r.get("account_name"))[:22]); continue   # offline recompute would drag — needs a sweep
    hlo = (ai.get("deal_scores") or {}).get("headline", {})
    ow, om = hlo.get("win_position"), hlo.get("deal_momentum")
    sc = SC.compute_deal_scores(rec)
    nw = (sc.get("headline") or {}).get("win_position"); nm = (sc.get("headline") or {}).get("deal_momentum")
    if nw is None:
        skip_noscore += 1; continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    p = CRO.build_cro_panel(rec)
    if p:
        sc["cro_panel"] = p
    out[id15(r["opp_id"])] = sc
    nmn = str(r.get("account_name"))[:24]
    if isinstance(ow, (int, float)) and isinstance(nw, (int, float)) and abs(ow - nw) >= 3:
        movesW.append((round(nw - ow, 1), nmn, ow, nw))
    if isinstance(om, (int, float)) and isinstance(nm, (int, float)) and abs(om - nm) >= 3:
        movesM.append((round(nm - om, 1), nmn, om, nm))

print(f"forecasted rescore: {len(out)} deals (have footprints) | skip pinned {skip_pin} | "
      f"skip NO-footprints {skip_nofp} (need a re-sweep) | skip no-score {skip_noscore}")
if needs_sweep:
    print("  need re-sweep (no footprints, would drag offline):", ", ".join(needs_sweep[:12]))
print("  WIN moves (>=3):")
for d, nm, o, n in sorted(movesW, key=lambda x: x[0])[:12]:
    print(f"    {d:+6}  {nm:24} {o} -> {n}")
print("  MOMENTUM moves (>=3):")
for d, nm, o, n in sorted(movesM, key=lambda x: x[0])[:14]:
    flag = "  <-- big drag, check footprints" if d <= -25 else ""
    print(f"    {d:+6}  {nm:24} {o} -> {n}{flag}")

# spotlight SAMI: number + rebuilt momentum panel text must agree
for oid, sc in out.items():
    hl = sc.get("headline") or {}
    for b in (sc.get("cro_panel") or {}).get("blocks") or []:
        if b.get("key") == "deal_momentum" and any("sami" in str(x).lower() for x in [b.get("summary", "")]):
            pass
print("\n  SAMI after rescore:")
sami = [(k, v) for k, v in out.items()]
for r in rows:
    if "sami" in str(r.get("account_name") or "").lower() and id15(r["opp_id"]) in out:
        sc = out[id15(r["opp_id"])]; hl = sc.get("headline") or {}
        print(f"    momentum={hl.get('deal_momentum')}  win={hl.get('win_position')}")
        for b in (sc.get("cro_panel") or {}).get("blocks") or []:
            if b.get("key") == "deal_momentum":
                for bl in (b.get("bullets") or [])[:5]:
                    print(f"      [{bl.get('tone')}] {(bl.get('text') or '')[:82]}")

only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
if only:
    keep = {id15(r["opp_id"]) for r in rows if only.lower() in str(r.get("account_name") or "").lower()}
    out = {k: v for k, v in out.items() if k in keep}
    print(f"\n[--only={only}] restricting apply to {len(out)} deal(s)")

if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()
items = list(out.items()); n = 0
for i in range(0, len(items), 40):
    blob = json.dumps(dict(items[i:i + 40]))
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), updated_at=now() "
           "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
    resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=150)
    n += len(resp.json()) if resp.status_code < 300 else 0
print(f"\nAPPLIED: {n} forecasted deal scores realigned")
