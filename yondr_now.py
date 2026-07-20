"""Re-sweep Yondr NOW and confirm it persists to the DB. The prior run failed on a
cold-start race (MCP tools not yet loaded on a freshly-scaled task, failed in 3.4s).
Strategy: fire POST /sweep/{oid}; if it fast-fails (MCP-not-loaded), retry immediately
(ALB round-robins to another task, which is warm); if it runs long, poll the DB until
Yondr shows a fresh v10.8+ai record. Prints the score when done."""
import sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
OID = "006P700000YjU3D"  # Yondr Group Limited


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def rec():
    try:
        r = requests.get(f"{SB}/rest/v1/deal_records",
                         params={"select": "updated_at,scores:record->ai->deal_scores,"
                                 "studio:record->ai->scoring_studio", "opp_id": f"eq.{OID}"},
                         headers=SH, verify=False, timeout=(10, 60)).json()
    except Exception:
        return None
    if not r:
        return None
    r = r[0]; ds = r.get("scores") or {}; hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
            "read": hl.get("read"), "src": ds.get("factor_source"), "eng": sv.get("win"),
            "present": bool(ds)}


base = rec()
bu = (base or {}).get("upd")
print(f"[{ts()}] Yondr baseline updated_at={bu}", flush=True)

for attempt in range(1, 9):
    print(f"[{ts()}] attempt {attempt}: POST /sweep/{OID} …", flush=True)
    t0 = time.time()
    fast_fail = False
    try:
        resp = requests.post(f"{API}/api/deal-engine/sweep/{OID}", headers=AH, json={},
                             verify=False, timeout=(10, 1400))
        dt = time.time() - t0
        try:
            body = resp.json()
        except Exception:
            body = {}
        st = (body or {}).get("status")
        err = (body or {}).get("error") or ""
        print(f"[{ts()}]   -> HTTP {resp.status_code} status={st} dt={dt:.1f}s err={str(err)[:120]}", flush=True)
        # cold-start / MCP-not-loaded fast failure -> retry a different task
        if dt < 60 and (st != "completed") and ("mcp" in str(err).lower() or "tools loaded" in str(err).lower() or st == "failed" or resp.status_code >= 500):
            fast_fail = True
    except Exception as e:  # ALB idle cut the long request; sweep continues server-side
        print(f"[{ts()}]   -> request severed ({type(e).__name__}); sweep likely running server-side", flush=True)

    if fast_fail:
        print(f"[{ts()}]   cold-start fast-fail — retrying immediately on another task", flush=True)
        time.sleep(3)
        continue

    # poll DB for a fresh v10.8+ai record
    print(f"[{ts()}]   polling DB for fresh Yondr record …", flush=True)
    t1 = time.time()
    landed = False
    while time.time() - t1 < 1500:
        time.sleep(20)
        a = rec()
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            print(f"\n[{ts()}] {'OK ' if ok else 'CHK'} Yondr Group Limited  win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} [{a['read']}]", flush=True)
            landed = True
            break
    if landed:
        break
    print(f"[{ts()}]   poll window elapsed with no fresh record; re-firing", flush=True)

fin = rec()
print(f"\n=== YONDR FINAL: win={ (fin or {}).get('win') } mom={ (fin or {}).get('mom') } "
      f"v{ (fin or {}).get('eng') } src={ (fin or {}).get('src') } "
      f"updated_at={ (fin or {}).get('upd') } ===")
print("YONDR-DONE")
