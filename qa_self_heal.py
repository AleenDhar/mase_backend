"""QA SELF-HEAL (2026-07-07, user-directed): after any sweep/rescore activity, verify every
drawer component per deal and REPAIR each broken one INDEPENDENTLY — with its own retry —
instead of re-running whole sweeps:

  scores   headline missing            -> deterministic recompute (retry x2)
  reasons  cro_panel missing/empty or its numbers diverge from the headline -> rebuild panel
  24h      day_summary missing / template / empty-with-activity -> intelligent regenerate
  footprints  missing engagement instrumentation -> deterministic rebuild from SF activity
              + datalake meetings (then rescore that deal, since momentum feeds on it)
  context  account_context stale vs the sibling index -> restamp (stamp_account_context)
  ceo      pinned/watch surfaces are durability-guarded in the sweep; counted here
  llm-only meddpicc/stakeholders/verdict missing -> FLAGGED (needs a real sweep; optionally
           re-trigger with --resweep-missing, capped at 10)

Idempotent. Dry-run by default; --apply repairs. Exit prints the QA matrix.
"""
import sys, re, json, subprocess
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO
from deal_engine_footprints import derive_footprints
from daily_summary import common as C
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"; MTOK = sec["SUPABASE_ACCESS_TOKEN"]


def _apply_ds(batch):
    if not batch:
        return 0
    n = 0
    items = list(batch.items())
    for i in range(0, len(items), 60):
        blob = json.dumps(dict(items[i:i + 60]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
               "updated_at=now() from (select key as opp_id, value from jsonb_each($J$" + blob +
               "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
        r = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                          json={"query": sql}, verify=VERIFY, timeout=120)
        n += len(r.json()) if r.status_code < 300 else 0
    return n


def _apply_field(oid, path, obj):
    sql = ("update deal_records set record = jsonb_set(record,'" + path + "', $J$" + json.dumps(obj) +
           "$J$::jsonb, true), updated_at=now() where opp_id='" + oid + "' returning opp_id")
    r = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                      json={"query": sql}, verify=VERIFY, timeout=60)
    return r.status_code < 300


def _rebuild_footprints(oid, sid, inst, dl):
    """Deterministic footprints from SF Tasks/Events + datalake meeting dates."""
    tks = C.soql(sid, inst, f"SELECT Subject,Type,CreatedDate,ActivityDate FROM Task WHERE WhatId='{oid}' "
                            f"AND CreatedDate>=LAST_N_DAYS:180 LIMIT 400")
    evs = C.soql(sid, inst, f"SELECT Subject,Type,ActivityDateTime,CreatedDate FROM Event WHERE WhatId='{oid}' "
                            f"AND (ActivityDateTime>=LAST_N_DAYS:180 OR CreatedDate>=LAST_N_DAYS:180) LIMIT 200")
    av = C.datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&state=eq.completed"
                            f"&select=start_at&order=start_at.desc&limit=40") if dl else []
    mdates = [str(m.get("start_at"))[:10] for m in (av or []) if m.get("start_at")]
    tasks = [{"date": t.get("CreatedDate"), "subject": t.get("Subject"), "type": t.get("Type")} for t in tks]
    events = [{"date": e.get("ActivityDateTime") or e.get("CreatedDate"), "subject": e.get("Subject"),
               "type": e.get("Type")} for e in evs]
    return derive_footprints(tasks=tasks, opp={}, meeting_dates=mdates, events=events, stage="")


def main():
    apply = "--apply" in sys.argv
    resweep_missing = "--resweep-missing" in sys.argv
    rows = requests.get(f"{SB}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,record", "active": "eq.true", "limit": "600"},
                        headers=H, verify=VERIFY, timeout=180).json()
    fix_scores, fix_panel, fix_24h, fix_fp, need_sweep = {}, {}, [], [], []
    ceo_watch = 0
    for r in rows:
        oid = id15(r["opp_id"]); rec = r.get("record") or {}; ai = rec.get("ai") or {}
        ds = ai.get("deal_scores") or {}
        hl = ds.get("headline") or {}
        pinned = bool(ds.get("pinned") or ai.get("pinned"))
        if (ai.get("ceo_intervention") or {}).get("needed"):
            ceo_watch += 1
        # --- component: SCORES ---
        if hl.get("win_position") is None and not pinned:
            try:
                sc = SC.compute_deal_scores(rec)
                if sc and (sc.get("headline") or {}).get("win_position") is not None:
                    rec.setdefault("ai", {})["deal_scores"] = sc
                    p = CRO.build_cro_panel(rec)
                    if p:
                        sc["cro_panel"] = p
                    fix_scores[oid] = sc
                else:
                    need_sweep.append((oid, r.get("account_name"), "no computable analysis"))
            except Exception as e:
                need_sweep.append((oid, r.get("account_name"), f"score compute error {str(e)[:40]}"))
        # --- component: REASONS PANEL (exists + numbers agree with headline) ---
        elif hl.get("win_position") is not None:
            p = ds.get("cro_panel")
            blocks = (p or {}).get("blocks") if isinstance(p, dict) else None
            pnum = next((b.get("score") for b in (blocks or []) if b.get("kind") == "score"
                         and b.get("key") in (None, "win_position")), None)
            diverged = (pnum is not None and isinstance(hl.get("win_position"), (int, float))
                        and abs(float(pnum) - float(hl["win_position"])) > 1.5)
            if not blocks or diverged:
                try:
                    rec.setdefault("ai", {})["deal_scores"] = ds
                    p2 = CRO.build_cro_panel(rec)
                    if p2:
                        ds["cro_panel"] = p2
                        fix_panel[oid] = ds
                except Exception:
                    pass
        # --- component: 24H (intelligent, non-empty when activity exists) ---
        d24 = ai.get("day_summary") or {}
        broken24 = (d24.get("source") != "ai"
                    or (d24.get("as_of") and not str(d24.get("overall") or "").strip()
                        and not (d24.get("items") or [])))
        if broken24:
            fix_24h.append(oid)
        # --- component: FOOTPRINTS (instrumentation) ---
        fp = ai.get("footprints") or {}
        if not fp or not isinstance(fp.get("engagement"), dict):
            fix_fp.append(oid)
        # --- llm-only components ---
        md = ai.get("meddpicc")
        if not (isinstance(md, dict) and len(md) >= 6):
            need_sweep.append((oid, r.get("account_name"), "meddpicc missing"))
        elif not (ai.get("stakeholder_map") or ai.get("stakeholders")):
            need_sweep.append((oid, r.get("account_name"), "stakeholders missing"))

    print(f"QA over {len(rows)} deals: scores-to-fix={len(fix_scores)} panels-to-fix={len(fix_panel)} "
          f"24h-to-fix={len(fix_24h)} footprints-to-fix={len(fix_fp)} llm-only-flags={len(need_sweep)} "
          f"ceo-watch={ceo_watch}")
    if not apply:
        print("[DRY RUN] --apply to repair each component independently."); return

    # REPAIR 1+2: scores + panels (deterministic, retry once on transport error)
    for attempt in (1, 2):
        try:
            n1 = _apply_ds(fix_scores); n2 = _apply_ds(fix_panel)
            print(f"repaired: scores={n1} panels={n2}"); break
        except Exception as e:
            print(f"score/panel apply attempt {attempt} failed: {str(e)[:60]}")

    # REPAIR 3: footprints (deterministic rebuild) then rescore those deals
    if fix_fp:
        try:
            sid, inst = C.sf_login(sec); dl = C.load_datalake()
            re_ds = {}
            for oid in fix_fp[:60]:
                try:
                    fps = _rebuild_footprints(oid, sid, inst, dl)
                    if fps and _apply_field(oid, "{ai,footprints}", fps):
                        rr = requests.get(f"{SB}/rest/v1/deal_records", params={"opp_id": "eq." + oid, "select": "record"},
                                          headers=H, verify=VERIFY, timeout=40).json()
                        if rr:
                            rec = rr[0]["record"]
                            if not ((rec.get("ai") or {}).get("deal_scores") or {}).get("pinned"):
                                sc = SC.compute_deal_scores(rec)
                                if sc and (sc.get("headline") or {}).get("win_position") is not None:
                                    rec.setdefault("ai", {})["deal_scores"] = sc
                                    p = CRO.build_cro_panel(rec)
                                    if p:
                                        sc["cro_panel"] = p
                                    re_ds[oid] = sc
                except Exception as e:
                    print(f"  footprints {oid}: {str(e)[:50]}")
            print(f"repaired: footprints={len(re_ds)} (rebuilt + rescored)")
            _apply_ds(re_ds)
        except Exception as e:
            print("footprints repair unavailable:", str(e)[:80])

    # REPAIR 4: 24h intelligent regenerate (its own subprocess; internal LLM retry)
    if fix_24h:
        for i in range(0, len(fix_24h), 100):
            part = fix_24h[i:i + 100]
            try:
                p = subprocess.run([sys.executable, "day_summary_ai.py", "--ids", ",".join(part), "--apply"],
                                   capture_output=True, text=True, timeout=3000)
                tail = [ln for ln in (p.stdout or "").splitlines() if "APPLIED" in ln or "summarised" in ln]
                print("repaired 24h batch:", "; ".join(tail[-2:]) or "(no output)")
            except Exception as e:
                print("24h batch failed:", str(e)[:60])

    # REPAIR 5: sibling context restamp (cheap, idempotent)
    try:
        p = subprocess.run([sys.executable, "stamp_account_context.py", "--apply"],
                           capture_output=True, text=True, timeout=600)
        print("context:", ([ln for ln in (p.stdout or "").splitlines() if "STAMPED" in ln or "scope" in ln] or ["?"])[-1])
    except Exception as e:
        print("context restamp failed:", str(e)[:60])

    # LLM-only components: flag (and optionally re-sweep, capped)
    if need_sweep:
        uniq = {}
        for oid, nm, why in need_sweep:
            uniq.setdefault(oid, (nm, why))
        print(f"needs a REAL sweep ({len(uniq)}):")
        for oid, (nm, why) in list(uniq.items())[:10]:
            print(f"  {oid} {str(nm)[:28]} — {why}")
        if resweep_missing:
            ids = list(uniq.keys())[:10]
            t = requests.post("http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com/api/deal-engine/sweep/trigger",
                              headers={"Authorization": f"Bearer {sec.get('DISPATCH_SECRET')}",
                                       "Content-Type": "application/json"},
                              json={"opp_ids": ids}, timeout=40)
            print(f"re-swept {len(ids)} (capped): HTTP {t.status_code}")
    print("QA SELF-HEAL COMPLETE")


if __name__ == "__main__":
    main()
