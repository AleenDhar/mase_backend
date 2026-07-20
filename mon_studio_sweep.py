"""Poll the sweep_queue + deal_records for the 3 test opps until each is scored by the
Studio-governed AI scorer (factor_source == 'ai'). Shows queue status transitions
(waiting -> working -> done) so a stalled worker is visible. Read-only."""
import sys, time, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
OPPS = [("006P700000Xl06R", "Publicis"), ("006P700000AfJyb", "Galp"), ("006P700000KHd9V", "John Deere")]


def qstatus(oid):
    try:
        r = requests.get(f"{SB}/rest/v1/sweep_queue",
                         params={"select": "status,attempts,error", "opp_id_15": f"eq.{oid}",
                                 "order": "created_at.desc", "limit": "1"},
                         headers=SH, verify=VERIFY, timeout=60).json()
        return (r[0].get("status"), r[0].get("attempts"), r[0].get("error")) if r else (None, None, None)
    except Exception as e:
        return (f"ERR{str(e)[:20]}", None, None)


def rec(oid):
    try:
        r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record,swept_at", "opp_id": f"eq.{oid}"},
                         headers=SH, verify=VERIFY, timeout=60).json()
        ai = (r[0]["record"].get("ai") or {}) if r else {}
        ds = ai.get("deal_scores") or {}
        hl = ds.get("headline") or {}
        return hl.get("win_position"), hl.get("deal_momentum"), ds.get("factor_source"), str(r[0]["swept_at"])[:16]
    except Exception:
        return None, None, None, "?"


deadline = time.time() + 18 * 60
done = set()
while time.time() < deadline and len(done) < 3:
    parts = []
    for oid, nm in OPPS:
        st, att, err = qstatus(oid)
        w, m, fs, sw = rec(oid)
        if fs == "ai":
            done.add(oid)
        tag = " [AI✓]" if fs == "ai" else ""
        errtag = f" ERR:{str(err)[:30]}" if (st == "failed" and err) else ""
        parts.append(f"{nm}: q={st}{('/a'+str(att)) if att else ''} win={w} mom={m} src={fs} sw={sw}{tag}{errtag}")
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] " + " | ".join(parts), flush=True)
    if len(done) >= 3:
        break
    time.sleep(40)
print(f"DONE: {len(done)}/3 scored by the Studio-governed AI scorer (factor_source=ai)")
