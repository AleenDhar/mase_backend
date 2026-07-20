"""Re-sweep Birmingham on the DEPLOYED new logic (rev 295: from-scratch default +
top-10 activity deep-read + new prompts) via the regular /sweep/{oid}, then dump the
full record for a quality check."""
import sys, time, warnings, datetime, threading, json
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


def full():
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "updated_at,record", "opp_id": f"eq.{OID}"},
                     headers=SH, verify=False, timeout=40).json()
    return (r[0]["updated_at"], r[0]["record"]) if r else (None, None)


bu, _ = full()
print(f"[{ts()}] firing regular /sweep/{OID} (default = from-scratch now)…", flush=True)
res = {"r": None}
def fire():
    try:
        rr = requests.post(f"{API}/api/deal-engine/sweep/{OID}", headers=AH, json={}, verify=False, timeout=(10, 1500))
        res["r"] = rr.json() if rr.status_code < 300 else f"HTTP {rr.status_code}: {rr.text[:200]}"
    except Exception as e:
        res["r"] = f"{type(e).__name__}: {e}"
th = threading.Thread(target=fire, daemon=True); th.start()

t0 = time.time(); rec = None
while time.time() - t0 < 1500:
    time.sleep(30)
    u, r = full()
    if u and u != bu and (r.get("ai") or {}).get("deal_scores"):
        rec = r; print(f"[{ts()}] landed (upd={u[:19]}).", flush=True); break
    if res["r"] is not None:
        time.sleep(5); u, rec = full(); break
print(f"[{ts()}] POST: {res['r']}", flush=True)
if not rec:
    print("no record"); print("BHAM-QC-DONE"); raise SystemExit(0)

json.dump(rec, open("cc_work/_bham_qc.json", "w", encoding="utf-8"), indent=1, default=str)
ai = rec.get("ai") or {}; hard = rec.get("hard") or {}; cov = rec.get("evidence_coverage") or {}
ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}


def items(x):
    return x.get("items") if isinstance(x, dict) else (x if isinstance(x, list) else [])


reqs = items(ai.get("explicit_requirements"))
def is_rfi(t):
    t = str(t or "").lower(); return ("rfi" in t or "response" in t) and ("submit" in t or "respond" in t)
rfi_open = [x.get("requirement") for x in reqs if is_rfi(x.get("requirement")) and not x.get("addressed")]
impl = ai.get("implicit_requirements") or {}
comm = items(impl.get("we_promised", {})) or items(impl)
moves = items(ai.get("recommended_moves"))
cp = ai.get("competitive_position") or {}
comps = cp.get("items") or cp.get("competitors") or []
daysum = ai.get("day_summary") or {}
medd = ai.get("meddpicc") or {}

print("\n" + "=" * 92)
print(f"EVIDENCE: calls_read={cov.get('calls_read')} | activities_deep={cov.get('activities_read') or cov.get('activities_deep_read') or 'n/a'} | gaps={len(cov.get('gaps') or [])}")
print(f"SCORE: win={hl.get('win_position')} mom={hl.get('deal_momentum')} read={hl.get('read')} eng=v{(ai.get('scoring_studio') or {}).get('versions',{}).get('win')}")
print(f"24h SUMMARY: as_of={daysum.get('as_of')} src={daysum.get('source')}")
print(f"   overall: {str(daysum.get('overall'))[:240]}")
print(f"\nREQUIREMENTS ({len(reqs)}) | STALE 'submit RFI' still open = {len(rfi_open)} -> {rfi_open}")
print(f"COMMITMENTS ({len(comm)}):")
for c in comm[:8]:
    print(f"   - [{c.get('status')}] {str(c.get('deliverable') or c.get('commitment') or c.get('inferred_need'))[:74]}")
print(f"MOVES ({len(moves)}):")
for m in moves[:6]:
    print(f"   - {str(m.get('action'))[:82]}")
comp_str = ", ".join(str(c.get("name")) + "(" + str(c.get("status") or c.get("threat_level") or "?") + ")" for c in comps[:8])
print(f"COMPETITORS ({len(comps)}): {comp_str}")
print(f"MEDDPICC champion={str((medd.get('champion') or {}).get('status'))[:24]} | EB={str((medd.get('economic_buyer') or {}).get('status'))[:24]}")
print(f"GAPS: {cov.get('gaps')}")
print("=" * 92)
print("saved cc_work/_bham_qc.json")
print("BHAM-QC-DONE")
