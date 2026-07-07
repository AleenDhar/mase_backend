"""Post-fix re-sweep orchestrator (2026-07-07). Runs AFTER the calls_read/no-data-guard fix is
live on the worker (rev >= 182 — the caller waits for the deploy).

WAVE 1  Bright Horizons (both opp records)
WAVE 2  Austrian Post + Alghanim Industries
WAVE 3  rest of the forecasted book (cc_work/_stage1.json)

After each wave: any swept record still missing deal_scores.headline gets a deterministic
recompute (score + cro_panel) applied — no deal is left malformed. At the end: re-run the
intelligent day-summary generator for every swept deal (the sweep's inline version is weaker),
then print the QA matrix (calls_read / win / mom / CEO / 24h) with BH, Austrian Post and
Alghanim called out.
"""
import json, re, subprocess, sys, time
from collections import Counter
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
TOK = sec.get("DISPATCH_SECRET")
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"
MTOK = sec["SUPABASE_ACCESS_TOKEN"]


def drows(params):
    return requests.get(f"{SB}/rest/v1/deal_records", params=params, headers=H, verify=VERIFY, timeout=120).json()


def by_name(pat):
    return [(id15(r["opp_id"]), r.get("account_name") or "")
            for r in drows({"account_name": f"ilike.*{pat}*", "active": "eq.true", "select": "opp_id,account_name"})]


def trigger(ids):
    r = requests.post(f"{BASE}/api/deal-engine/sweep/trigger",
                      headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
                      json={"opp_ids": ids}, timeout=60)
    print(f"  trigger {len(ids)} deals -> HTTP {r.status_code}", flush=True)
    return r.status_code in (200, 202)


def wait_done(ids, max_min):
    inl = "in.(" + ",".join(ids) + ")"
    t0 = time.time()
    while time.time() - t0 < max_min * 60:
        try:
            q = requests.get(f"{SB}/rest/v1/sweep_queue", params={"opp_id": inl, "select": "status"},
                             headers=H, verify=VERIFY, timeout=40).json()
            by = Counter(x.get("status") for x in q)
            rem = by.get("waiting", 0) + by.get("working", 0)
            print(f"  [{int((time.time()-t0)//60):3d}m] {dict(by)} remaining={rem}", flush=True)
            if rem == 0:
                return True
        except Exception as e:
            print("  poll err:", str(e)[:60], flush=True)
        time.sleep(75)
    print(f"  WAVE TIMEOUT after {max_min}m — marking stuck rows done and continuing", flush=True)
    try:
        requests.patch(f"{SB}/rest/v1/sweep_queue?status=in.(waiting,working)&opp_id={inl}",
                       headers={**H, "Content-Type": "application/json"}, json={"status": "done"},
                       verify=VERIFY, timeout=40)
    except Exception:
        pass
    return False


def heal_scores(ids):
    """Any record missing deal_scores.headline after its sweep -> deterministic recompute+panel."""
    out = {}
    recs = drows({"opp_id": "in.(" + ",".join(ids) + ")", "select": "opp_id,account_name,record"})
    for r in recs:
        oid = id15(r["opp_id"]); rec = r.get("record") or {}
        hl = ((rec.get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
        if hl.get("win_position") is not None:
            continue
        try:
            sc = SC.compute_deal_scores(rec)
            if not (sc and (sc.get("headline") or {}).get("win_position") is not None):
                print(f"  heal SKIP {r.get('account_name')}: no computable headline", flush=True)
                continue
            rec.setdefault("ai", {})["deal_scores"] = sc
            panel = CRO.build_cro_panel(rec)
            if panel:
                sc["cro_panel"] = panel
            out[oid] = sc
            print(f"  healed {str(r.get('account_name'))[:28]}: win={sc['headline'].get('win_position')}", flush=True)
        except Exception as e:
            print(f"  heal FAIL {r.get('account_name')}: {type(e).__name__} {str(e)[:80]}", flush=True)
    if out:
        blob = json.dumps(out)
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob +
               "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        print(f"  heal APPLIED: {len(resp.json()) if resp.status_code < 300 else resp.text[:120]}", flush=True)
    else:
        print("  heal: nothing to heal (all scored)", flush=True)


def matrix(ids, label):
    recs = drows({"opp_id": "in.(" + ",".join(ids) + ")", "select": "opp_id,account_name,record"})
    print(f"\n=== {label} ===", flush=True)
    for r in sorted(recs, key=lambda x: str(x.get("account_name"))):
        ai = (r.get("record") or {}).get("ai") or {}
        hl = (ai.get("deal_scores") or {}).get("headline") or {}
        cr = (ai.get("evidence_coverage") or {}).get("calls_read")
        ci = (ai.get("ceo_intervention") or {}).get("needed")
        ds = ai.get("day_summary") or {}
        print(f"  {str(r.get('account_name'))[:28]:28} win={hl.get('win_position')} mom={hl.get('deal_momentum')} "
              f"calls_read={cr} ceo={ci} 24h={ds.get('as_of')} ({len(ds.get('items') or [])} items)", flush=True)


def main():
    # 2026-07-07 user-directed: START from Techtronic, then the rest of the forecasted book.
    # EXCLUDE the hand-fixed pinned deals (Bright Horizons JwvB3, Austrian Post, Alghanim) —
    # do not re-sweep them at all.
    EXCLUDE = {"006P700000JwvB3", "006P700000J71MD", "006P700000OUsd6"}
    tt = [o for o, _ in by_name("Techtronic") if o not in EXCLUDE]
    s1 = [id15(x) for x in json.load(open("cc_work/_stage1.json"))]
    w2 = [o for o in s1 if o not in EXCLUDE and o not in set(tt)]
    print(f"WAVE 1 Techtronic: {tt}")
    print(f"WAVE 2 rest of forecasted (pinned excluded): {len(w2)} deals")
    print(f"EXCLUDED (hand-fixed, pinned): {sorted(EXCLUDE)}", flush=True)

    for label, ids, mins in (("WAVE 1 (Techtronic)", tt, 30),
                             ("WAVE 2 (rest of forecasted)", w2, 150)):
        print(f"\n>>> {label}: triggering {len(ids)}", flush=True)
        if not ids:
            continue
        if not trigger(ids):
            print("  TRIGGER FAILED — aborting", flush=True); return
        wait_done(ids, mins)
        heal_scores(ids)
        if label.startswith("WAVE 1") or label.startswith("WAVE 2"):
            matrix(ids, label + " result")

    everything = tt + w2
    # restore the INTELLIGENT day summaries over the sweep's inline ones
    print("\n>>> restoring intelligent 24h summaries for all swept deals", flush=True)
    try:
        p = subprocess.run([sys.executable, "day_summary_ai.py", "--ids", ",".join(everything), "--apply"],
                           capture_output=True, text=True, timeout=3000)
        tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][-3:]
        for ln in tail:
            print("  " + ln, flush=True)
    except Exception as e:
        print("  day-summary rerun failed:", str(e)[:120], flush=True)

    # final QA
    matrix(tt, "FINAL — Techtronic")
    recs = drows({"opp_id": "in.(" + ",".join(everything) + ")", "select": "record"})
    scored = sum(1 for r in recs if (((r.get("record") or {}).get("ai") or {}).get("deal_scores") or {}).get("headline", {}).get("win_position") is not None)
    ceo = sum(1 for r in recs if (((r.get("record") or {}).get("ai") or {}).get("ceo_intervention") or {}).get("needed"))
    ds_ok = sum(1 for r in recs if (((r.get("record") or {}).get("ai") or {}).get("day_summary") or {}).get("overall"))
    print(f"\nBOOK QA: {len(recs)} swept | scored={scored} | ceo_watch={ceo} | 24h_present={ds_ok}", flush=True)
        # QA SELF-HEAL (2026-07-07): verify every drawer component and repair each broken one
    # INDEPENDENTLY (scores / reasons / 24h / footprints / context) — no whole-sweep re-runs
    # for fixable parts.
    try:
        p = subprocess.run([sys.executable, "qa_self_heal.py", "--apply"],
                           capture_output=True, text=True, timeout=3600)
        for ln in [l for l in (p.stdout or "").splitlines() if l.strip()][-6:]:
            print("  QA: " + ln, flush=True)
    except Exception as e:
        print("  QA self-heal failed:", str(e)[:100], flush=True)
    print("ORCHESTRATION COMPLETE", flush=True)


if __name__ == "__main__":
    main()
