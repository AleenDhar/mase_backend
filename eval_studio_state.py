"""EVAL helper — read the live Scoring Studio state via the DEPLOYED API.
Read-only. Prints active locked versions, trails, and any unlocked drafts."""
import json, os, sys
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
BASE = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
H = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}


def get(path):
    r = requests.get(f"{BASE}/api/deal-engine/{path}", headers=H, verify=False, timeout=60)
    print(f"GET {path} -> {r.status_code}")
    return r.json() if r.status_code == 200 else {"error": r.text[:300]}


eng = get("scoring-studio/engines")
for e in eng.get("engines", []):
    a = e.get("active") or {}
    print(f"  {e['engine']:8s} active v{a.get('version')} locked_by={a.get('locked_by')} "
          f"at={a.get('locked_at')} | has_draft={e.get('has_draft')} versions={e.get('versions')}")

tr = get("scoring-studio/mom/trail")
print("mom trail:")
for r_ in tr.get("trail", []):
    print(f"  v{r_['version']:5s} kind={r_['kind']:7s} locked={r_['locked']} by={r_.get('locked_by')} at={r_.get('locked_at')}")
    print(f"        note: {(r_.get('note') or '')[:140]}")
d = tr.get("draft")
print(f"mom draft: {json.dumps(d, indent=2) if d else 'NONE'}")
if d:
    dv = get("scoring-studio/mom/version/draft")
    c = dv.get("content") or ""
    print(f"draft content: {len(c)} chars, head:\n{c[:600]}")
    with open("eval_user_draft_mom_backup.txt", "w", encoding="utf-8") as f:
        f.write(c)
    print("[saved draft backup -> eval_user_draft_mom_backup.txt]")

# also snapshot the CURRENT locked mom content (restore target for after the evals)
mv = get("scoring-studio/active")
act = (mv.get("active") or {}).get("mom") or {}
with open("eval_original_mom_locked.txt", "w", encoding="utf-8") as f:
    f.write(act.get("content") or "")
print(f"active mom v{act.get('version')} content saved -> eval_original_mom_locked.txt "
      f"({len(act.get('content') or '')} chars)")
