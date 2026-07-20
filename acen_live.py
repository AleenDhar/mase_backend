"""LIVE production sweep of ACEN (writes to deal_records) via the manual trigger, then WATCH
the deal_records row flip to the governed score. This is a REAL run (not dry-run) — the drawer
will update from the degraded 82 to the governed ~20/8. User-authorized."""
import time, sys
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OID = "006P700000DkWgX"


def rec():
    r = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                       params={"select": "swept_at,updated_at,record", "opp_id": f"eq.{OID}"},
                       headers=D.SH, verify=D.VERIFY, timeout=60).json()
    return r[0] if isinstance(r, list) and r else {}


def hl_of(row):
    ds = (((row.get("record") or {}).get("ai") or {}).get("deal_scores") or {})
    return ds.get("headline") or {}, ds.get("factor_source"), ds.get("scoring_degraded")


base = rec()
base_swept = base.get("swept_at")
bhl, bsrc, bdeg = hl_of(base)
print(f"BEFORE  swept_at={base_swept}  win={bhl.get('win_position')} mom={bhl.get('deal_momentum')} "
      f"src={bsrc} degraded={bdeg}", flush=True)

# --- LIVE trigger (writes to deal_records; source=manual runs synchronously in the web process) ---
r = D.requests.post(f"{D.API}/api/deal-engine/sweep/trigger",
                    headers={**D.AH, "Content-Type": "application/json"},
                    json={"opp_id": OID, "source": "manual"}, verify=False, timeout=60)
print(f"TRIGGER  HTTP {r.status_code}  {r.text[:220]}", flush=True)
if r.status_code >= 300:
    sys.exit(1)

# --- WATCH deal_records until swept_at advances ---
t0 = time.time()
while time.time() - t0 < 1500:
    time.sleep(45)
    cur = rec()
    cs = cur.get("swept_at")
    if cs and cs != base_swept:
        h, src, deg = hl_of(cur)
        print(f"\n✅ UPDATED  swept_at={cs}  WIN {h.get('win_position')} | MOM {h.get('deal_momentum')} | "
              f"read={h.get('read')} | src={src} degraded={deg}", flush=True)
        ds = (((cur.get("record") or {}).get("ai") or {}).get("deal_scores") or {})
        for b in (ds.get("ai_reasons", {}).get("win_position") or [])[:6]:
            print("   •", b.get("text"), flush=True)
        ceo = ((cur.get("record") or {}).get("ai") or {}).get("ceo_intervention") or {}
        print(f"   CEO needed={ceo.get('needed')} summary={ceo.get('summary')!r}", flush=True)
        break
    print(f"  [{int((time.time()-t0)//60)}m] running… (swept_at still {cs})", flush=True)
else:
    print("TIMEOUT — no deal_records update after 25 min; check the sweep dashboard.", flush=True)
