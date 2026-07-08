"""EVAL — save+lock a mom instruction variant via the deployed Studio API.
Usage: python eval_lock.py strict|loose|restore
Prints the new locked version + the expected effective-prompt fingerprint."""
import hashlib, json, sys
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODE = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
SRC = {"strict": "eval_variant_strict.txt", "loose": "eval_variant_loose.txt",
       "restore": "eval_original_mom_locked.txt"}[MODE]
NOTE = {
    "strict": ("EVAL QA probe A — STRICT reading: buyer-confirmed milestones only; Slowing default "
               "without buyer action ≤14d; defensive sessions at half depth; process-mode needs a dated "
               "buyer deliverable ≤30d. Temporary — superseded by the restore lock."),
    "loose": ("EVAL QA probe B — GENEROUS reading: any dated future milestone counts; 2+ sessions/45d "
              "reads as building; close pushes ≤90d neutral. Temporary — superseded by the restore lock."),
    "restore": ("RESTORE production doctrine — content identical to v10.2. Closes the strict/loose QA "
                "probes (archived as the two prior versions); results in Desktop eval_strict.csv / "
                "eval_loose.csv. See docs/cro-scoring-doctrine.md."),
}[MODE]

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
BASE = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
H = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
content = open(SRC, encoding="utf-8").read()

r = requests.post(f"{BASE}/api/deal-engine/scoring-studio/mom/draft", headers=H, verify=False,
                  json={"content": content, "author": "sam.thomas@zycus.com (claude QA eval)"}, timeout=60)
print(f"draft -> {r.status_code} {r.text[:200]}")
r.raise_for_status()
r = requests.post(f"{BASE}/api/deal-engine/scoring-studio/mom/lock", headers=H, verify=False,
                  json={"kind": "minor", "note": NOTE,
                        "locked_by": "sam.thomas@zycus.com (claude QA eval)"}, timeout=60)
print(f"lock  -> {r.status_code} {r.text[:300]}")
r.raise_for_status()
new_v = r.json().get("version")

# verify the resolver serves it + compute the expected effective-prompt fingerprint
act = requests.get(f"{BASE}/api/deal-engine/scoring-studio/active", headers=H, verify=False, timeout=60).json()
active = act.get("active") or {}
assert (active.get("mom") or {}).get("version") == new_v, f"resolver did not adopt v{new_v}!"
assert (active.get("mom") or {}).get("content") == content, "resolver content != what we locked!"

basep = requests.get(f"{BASE}/api/deal-engine/sweep/prompt", headers=H, verify=False, timeout=60).json()
base_txt = (basep.get("prompt") or "").strip() or (basep.get("default") or "")
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
fp = hashlib.sha256((base_txt + head + "\n\n".join(parts)).encode("utf-8")).hexdigest()[:12]
print(f"LOCKED mom v{new_v} ({MODE}) — resolver verified. expected worker fingerprint: #{fp}")
json.dump({"mode": MODE, "version": new_v, "fp": fp}, open(f"eval_lock_{MODE}.json", "w"))
