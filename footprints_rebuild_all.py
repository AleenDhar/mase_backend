"""Rebuild footprints (deterministic, DIRECT SOQL — not the flaky agent/MCP path) for EVERY
active deal whose footprints are missing/empty, then rescore it. Root cause of Nidec-class
'falsely cold' deals: the sweep's MCP footprints read returned empty, so momentum/win cratered
(win 5, mom 25) on deals that actually have heavy engagement (Nidec: 12 buyer touches/30d).
Parallel SF reads (8 threads). Pinned deals skipped. --apply writes."""
import sys, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SID, INST = C.sf_login(sec)
DL = C.load_datalake()


def rebuild_fp(oid, stage):
    tks = C.soql(SID, INST, f"SELECT Subject,Type,CreatedDate,ActivityDate FROM Task WHERE WhatId='{oid}' AND CreatedDate>=LAST_N_DAYS:180 LIMIT 400")
    evs = C.soql(SID, INST, f"SELECT Subject,Type,ActivityDateTime,CreatedDate FROM Event WHERE WhatId='{oid}' AND (ActivityDateTime>=LAST_N_DAYS:180 OR CreatedDate>=LAST_N_DAYS:180) LIMIT 200")
    opp = C.soql(SID, INST, f"SELECT Last_Email_Received_Date__c,Last_Meeting_Date__c,Next_Step_Updated_Date_Time__c,No_activity_in_last_20_30_Days__c,LastActivityDate FROM Opportunity WHERE Id='{oid}' LIMIT 1")
    av = C.datalake_get(DL, f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&state=eq.completed&select=start_at&order=start_at.desc&limit=40") if DL else []
    return derive_footprints(
        tasks=[{"date": t.get("CreatedDate") or t.get("ActivityDate"), "subject": t.get("Subject"), "type": t.get("Type")} for t in (tks or [])],
        opp=(opp[0] if opp else {}) or {},
        events=[{"date": e.get("ActivityDateTime") or e.get("CreatedDate"), "subject": e.get("Subject"), "type": e.get("Type")} for e in (evs or [])],
        meeting_dates=[str(m.get("start_at"))[:10] for m in (av or []) if m.get("start_at")], stage=stage or "")


def main():
    apply = "--apply" in sys.argv
    rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "opp_id,account_name,stage,record", "active": "eq.true", "limit": "600"},
                        headers=H, verify=VERIFY, timeout=180).json()
    targets = []
    for r in rows:
        ai = (r.get("record") or {}).get("ai") or {}
        fp = ai.get("footprints") or {}
        if (ai.get("deal_scores") or {}).get("pinned"):
            continue
        if not fp or not isinstance(fp.get("engagement"), dict) or (fp.get("last_buyer_touch") is None and fp.get("last_meeting") is None and not fp.get("buyer_touches_30d") and not fp.get("meetings_60d")):
            targets.append((id15(r["opp_id"]), r.get("account_name"), r.get("stage"), r.get("record")))
    print(f"active={len(rows)} | need footprints rebuild={len(targets)}")
    if not apply:
        print("[DRY RUN] --apply to rebuild + rescore."); return

    def work(t):
        oid, nm, stage, rec = t
        try:
            fps = rebuild_fp(oid, (rec.get("hard") or {}).get("stage") or stage)
            rec.setdefault("ai", {})["footprints"] = fps
            sc = SC.compute_deal_scores(rec)
            if not (sc and (sc.get("headline") or {}).get("win_position") is not None):
                return oid, None, None
            rec["ai"]["deal_scores"] = sc
            p = CRO.build_cro_panel(rec)
            if p:
                sc["cro_panel"] = p
            return oid, fps, sc
        except Exception as e:
            return oid, "ERR", str(e)[:50]

    fp_out, ds_out, errs = {}, {}, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, t) for t in targets]):
            oid, fps, sc = fut.result()
            if fps == "ERR" or fps is None:
                errs += 1; continue
            fp_out[oid] = fps; ds_out[oid] = sc
    print(f"rebuilt {len(fp_out)} | errors {errs}")
    for path, data in (("{ai,footprints}", fp_out), ("{ai,deal_scores}", ds_out)):
        items = list(data.items()); n = 0
        for i in range(0, len(items), 50):
            blob = json.dumps(dict(items[i:i + 50]))
            sql = ("update deal_records d set record = jsonb_set(record,'" + path + "', m.value, true), updated_at=now() "
                   "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
            resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                                 json={"query": sql}, verify=VERIFY, timeout=120)
            n += len(resp.json()) if resp.status_code < 300 else 0
        print(f"  applied {path}: {n}")


if __name__ == "__main__":
    main()
