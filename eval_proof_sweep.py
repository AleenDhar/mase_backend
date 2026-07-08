"""E2E PROOF — enqueue ONE small deal's sweep via the deployed API, so we can then
verify (a) ai.scoring_studio.versions stamps onto the record and (b) the worker's
prompt fingerprint matches base-prompt + LOCKED studio block. Read + one enqueue."""
import datetime, hashlib, json, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
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

sec = load_secret(); sb = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": key, "Authorization": f"Bearer {key}"}

# --- pick a SMALL deal: active, NOT forecasted, not pinned, low amount, early stage
rows = requests.get(f"{sb}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,amount,updated_at,"
                                      "record->ai->pinned,record->ai->scoring_studio",
                            "active": "eq.true", "forecast_critical": "eq.false",
                            "order": "amount.asc.nullsfirst", "limit": "25"},
                    headers=SH, verify=VERIFY, timeout=120).json()
pick = None
for r in rows:
    if r.get("pinned"):
        continue
    if (r.get("stage") or "").lower().startswith(("closed", "dead")):
        continue
    pick = r
    break
if not pick:
    print("no candidate found"); sys.exit(1)
print(f"PROOF DEAL: {pick['account_name']} — {pick['opp_name']} ({pick['opp_id']}) "
      f"stage={pick['stage']} amount={pick['amount']}")
print(f"  record.updated_at before: {pick['updated_at']}")
print(f"  prior scoring_studio stamp: {pick.get('scoring_studio')}")

# --- expected fingerprint of the effective prompt (base override + studio block),
#     computed EXACTLY as deal_engine_sweep._prompt_fingerprint does
basep = requests.get(f"{BASE}/api/deal-engine/sweep/prompt", headers=H, verify=False, timeout=60).json()
base_txt = (basep.get("prompt") or "").strip() or (basep.get("default") or "")
act = requests.get(f"{BASE}/api/deal-engine/scoring-studio/active", headers=H, verify=False, timeout=60).json()
active = act.get("active") or {}
ENGINES = ("extract", "win", "mom", "todo", "sum")
NAMES = {"extract": "Signal Extraction / Deal-Reading", "win": "Zycus Win Position",
         "mom": "Deal Momentum", "todo": "To-Do Generation", "sum": "24-Hour Summary"}
parts, versions = [], {}
for eng in ENGINES:
    row = active.get(eng)
    if not row:
        continue
    versions[eng] = row["version"]
    parts.append(f"### ENGINE — {NAMES[eng]} · LOCKED v{row['version']}\n\n{row['content']}")
head = ("\n\n# SCORING VERSION STUDIO — LOCKED ENGINE INSTRUCTIONS (AUTHORITATIVE)\n"
        "The five instructions below are the versioned, LOCKED governing instructions "
        "(edited in Omnivision). They are the CURRENT operating law for signal extraction, "
        "win-position reading, momentum reading, to-do generation and the 24-hour summary — "
        "where anything above conflicts with them, THESE WIN. Provenance: "
        + " · ".join(f"{e} v{v}" for e, v in versions.items()) + "\n\n")
effective = base_txt + head + "\n\n".join(parts)
first = (effective.splitlines()[0].strip() if effective else "")[:80]
fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:12]
base_fp = hashlib.sha256(base_txt.encode("utf-8")).hexdigest()[:12]
print(f"\nexpected EFFECTIVE prompt fingerprint: '{first}' #{fp}")
print(f"(base prompt alone would be #{base_fp} — if CloudWatch shows #{fp}, the studio block IS in the prompt)")
print(f"studio versions in block: {versions}")
open("eval_expected_fp.json", "w").write(json.dumps(
    {"fp": fp, "base_fp": base_fp, "versions": versions,
     "proof_opp": pick["opp_id"], "proof_name": pick["account_name"]}))

# --- enqueue the sweep
t0 = datetime.datetime.now(datetime.timezone.utc).isoformat()
r = requests.post(f"{BASE}/api/deal-engine/sweep/rerun", headers=H, verify=False,
                  json={"opp_id": pick["opp_id"]}, timeout=60)
print(f"\nenqueue at {t0} -> {r.status_code} {r.text[:200]}")
