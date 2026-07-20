"""Re-sweep Birmingham FROM SCRATCH (no living memory) via /update-living-memory, with
the new GROUND-TRUTH prompts live. Capture BEFORE vs AFTER on the exact contradictions:
stale 'submit RFI' duplicates, contradictory moves, 24h-summary staleness, win score."""
import sys, time, warnings, datetime, threading
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
OID = "006P700000X6W3q"


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def snap():
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "updated_at,record", "opp_id": f"eq.{OID}"},
                     headers=SH, verify=False, timeout=40).json()
    if not r:
        return None
    rec = r[0]["record"]; ai = rec.get("ai") or {}
    ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}
    reqs = ai.get("explicit_requirements")
    reqs = reqs.get("items") if isinstance(reqs, dict) else (reqs if isinstance(reqs, list) else [])
    def is_rfi_submit(t):
        t = str(t or "").lower()
        return ("rfi" in t or "response" in t) and ("submit" in t or "respond" in t)
    rfi_open = [x for x in reqs if is_rfi_submit(x.get("requirement")) and not x.get("addressed")]
    moves = ai.get("recommended_moves"); moves = moves.get("items") if isinstance(moves, dict) else (moves or [])
    daysum = ai.get("day_summary") or {}
    return {
        "upd": r[0]["updated_at"], "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
        "read": hl.get("read"), "eng": (ai.get("scoring_studio") or {}).get("versions", {}).get("win"),
        "req_total": len(reqs), "rfi_submit_open": len(rfi_open),
        "rfi_open_txt": [str(x.get("requirement"))[:55] for x in rfi_open],
        "moves": [str(m.get("action"))[:70] for m in moves[:5]],
        "ds_asof": daysum.get("as_of"), "ds_src": daysum.get("source"),
        "calls": (rec.get("evidence_coverage") or {}).get("calls_read"),
    }


def show(tag, s):
    if not s:
        print(f"[{tag}] no record"); return
    print(f"[{tag}] win={s['win']} mom={s['mom']} read={s['read']} eng=v{s['eng']} calls={s['calls']} upd={str(s['upd'])[:19]}")
    print(f"[{tag}] requirements={s['req_total']} | STALE 'submit RFI' still open={s['rfi_submit_open']} -> {s['rfi_open_txt']}")
    print(f"[{tag}] 24h summary as_of={s['ds_asof']} src={s['ds_src']}")
    print(f"[{tag}] moves:")
    for m in s["moves"]:
        print(f"        - {m}")


b = snap()
print("=" * 90); show("BEFORE", b); print("=" * 90, flush=True)
bu = (b or {}).get("upd")

res = {"r": None, "e": None}
def fire():
    try:
        rr = requests.post(f"{API}/api/deal-engine/sweep/{OID}/update-living-memory",
                           headers=AH, json={}, verify=False, timeout=(10, 1500))
        res["r"] = rr.json() if rr.status_code < 300 else f"HTTP {rr.status_code}: {rr.text[:200]}"
    except Exception as e:  # noqa: BLE001
        res["e"] = f"{type(e).__name__}: {e}"

print(f"[{ts()}] firing FROM-SCRATCH re-sweep (source=update_living_memory)…", flush=True)
th = threading.Thread(target=fire, daemon=True); th.start()
t0 = time.time()
while time.time() - t0 < 1500:
    time.sleep(30)
    a = snap()
    if a and a["upd"] != bu and a["win"] is not None:
        print(f"[{ts()}] landed.", flush=True)
        break
    if res["r"] is not None or res["e"] is not None:
        time.sleep(5); a = snap(); break
else:
    a = snap()

print(f"[{ts()}] POST result: {res['r'] or res['e']}", flush=True)
print("=" * 90); show("AFTER ", a); print("=" * 90)
if b and a:
    print(f"\nDELTA: win {b['win']} -> {a['win']} | stale 'submit RFI' open {b['rfi_submit_open']} -> {a['rfi_submit_open']} | "
          f"24h as_of {b['ds_asof']} -> {a['ds_asof']}")
print("BHAM-RERUN-DONE")
