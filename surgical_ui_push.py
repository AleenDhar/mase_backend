"""SURGICAL UI push (user-directed 2026-07-09): put the SCORE REASONS + 24-HOUR SUMMARY
into the deal drawer for every forecasted deal we already generated locally — WITHOUT
touching to-dos, stakeholders & risk, MEDDPICC, competitive, or anything else. It reads
the LIVE DB record as the base and overlays ONLY three keys from the good local sweep:
  ai.deal_scores          (headline scores + cro_panel reasons + ai_reasons + factor_source)
  ai.deal_scores_evidence (the deal-specific reason bullets)
  ai.day_summary          (the 24h summary)
Everything else in the record is preserved byte-for-byte. Then upsert.

Run:  python surgical_ui_push.py           (dry — lists what would change)
      python surgical_ui_push.py --push     (writes to deal_records)
"""
import sys, os, json, glob, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, sb_upsert
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PUSH = "--push" in sys.argv
sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
import deal_engine_cro as CRO

SKIP = {"006P700000Xl06R"}   # Publicis already fully re-pushed


def good_local():
    """opp_id -> local record that has scores + (panel or reasons) + day_summary."""
    out = {}
    for p in sorted(glob.glob("dryrun_forecasted/*.json")):
        if any(x in p for x in ("_tainted", "_pre", "_summary")):
            continue
        oid = os.path.basename(p)[:-5]
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        ai = d.get("ai") or {}
        ds = ai.get("deal_scores") or {}
        hl = ds.get("headline") or {}
        reasons = ((ds.get("ai_reasons") or {}).get("win_position")
                   or ((ai.get("deal_scores_evidence") or {}).get("ai_reasons") or {}).get("win_position"))
        if hl.get("win_position") is not None and ai.get("day_summary") and (ds.get("cro_panel") or reasons):
            out[oid] = d
    return out


def db_record(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "record", "opp_id": f"eq.{oid}"},
                     headers=H, verify=VERIFY, timeout=60).json()
    return (r[0]["record"] if r else None)


local = good_local()
print(f"local complete records: {len(local)}  (surgical reasons+24h push)")
rows, changes = [], []
now = datetime.datetime.now(datetime.timezone.utc)
for oid, src in local.items():
    if oid in SKIP:
        continue
    base = db_record(oid)
    if not base:
        print(f"  {oid}: not in deal_records — skip"); continue
    b_ai = base.setdefault("ai", {})
    s_ai = src.get("ai") or {}
    # --- overlay ONLY the three drawer surfaces (everything else stays as-is) ---
    new_ds = dict(s_ai.get("deal_scores") or {})
    if not (new_ds.get("cro_panel") or {}).get("blocks"):
        # ensure the reasons panel exists (UI reads cro_panel) — build on the merged view
        try:
            merged = {**base, "ai": {**b_ai, "deal_scores": new_ds,
                                     "deal_scores_evidence": s_ai.get("deal_scores_evidence")
                                     or b_ai.get("deal_scores_evidence")}}
            panel = CRO.build_cro_panel(merged)
            if panel:
                new_ds["cro_panel"] = panel
        except Exception as e:
            print(f"  {oid}: panel build skipped ({e})")
    before_panel = len(((b_ai.get("deal_scores") or {}).get("cro_panel") or {}).get("blocks") or [])
    before_win = ((b_ai.get("deal_scores") or {}).get("headline") or {}).get("win_position")
    b_ai["deal_scores"] = new_ds
    if s_ai.get("deal_scores_evidence"):
        b_ai["deal_scores_evidence"] = s_ai["deal_scores_evidence"]
    if s_ai.get("day_summary"):
        b_ai["day_summary"] = s_ai["day_summary"]
    if s_ai.get("scoring_studio"):
        b_ai["scoring_studio"] = s_ai["scoring_studio"]
    hard = base.get("hard") or {}
    hl = new_ds.get("headline") or {}
    changes.append((hard.get("account_name") or oid, before_win, hl.get("win_position"),
                    before_panel, len((new_ds.get("cro_panel") or {}).get("blocks") or []),
                    new_ds.get("factor_source")))
    rows.append({
        "opp_id": oid, "owner_name": hard.get("owner_name"), "account_name": hard.get("account_name"),
        "opp_name": hard.get("opp_name"), "stage": hard.get("stage"),
        "forecast_category": hard.get("forecast_category"), "amount": hard.get("amount"),
        "close_date": hard.get("close_date") or None, "qualified_date": hard.get("qualified_date") or None,
        "last_activity_date": hard.get("last_activity_date") or None,
        "forecast_critical": bool(base.get("forecast_critical")),
        "analysis_confidence": base.get("analysis_confidence"),
        "swept_at": base.get("swept_at"), "record": base,
        "updated_at": now.isoformat(),
    })

print(f"\n{'account':30} {'win(before->after)':>20} {'panel blk':>12} {'src':>6}")
for acct, bw, aw, bp, ap, src in sorted(changes, key=lambda x: str(x[0])):
    print(f"{str(acct)[:30]:30} {str(bw)+'->'+str(aw):>20} {str(bp)+'->'+str(ap):>12} {str(src):>6}")
print(f"\n{len(rows)} deals to update (reasons + 24h only; todos/stakeholders/deal-intel untouched)")

if not PUSH:
    print("\n[DRY] re-run with --push to write to deal_records")
    sys.exit(0)

# batch upsert (chunks of 10)
for i in range(0, len(rows), 10):
    sb_upsert(sec, "deal_records", rows[i:i + 10], on_conflict="opp_id")
    print(f"  pushed {min(i+10, len(rows))}/{len(rows)}")
print("\nPUSHED reasons + 24h to deal_records for", len(rows), "deals.")
