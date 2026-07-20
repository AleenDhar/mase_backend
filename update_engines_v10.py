"""Omnivision — add a PEOPLE vs PRODUCTS hard rule to the extract + sweep engines
(user directive 2026-07-13). Stops the recurring fabrication where a product/system name
('Oracle Cloud Fusion') lands in a person slot (to-do owner/target, stakeholder) and the
gate has to scrub it. Dry-run by default; --apply to lock. Bumps minor versions."""
import sys, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-people-vs-products-2026-07-13"

ANCHOR = "overwrite them with a guess or an older state."
MARKER = "## PEOPLE vs PRODUCTS"
BLOCK = """

## PEOPLE vs PRODUCTS — hard rule
A named individual in ANY to-do, move, action, requirement owner, stakeholder, champion or
economic-buyer slot MUST be a REAL HUMAN who appears in a Salesforce contact role, an Avoma
attendee list, or the active-user roster. **Products, platforms, ERPs, applications, modules
and vendor names are NOT people** — e.g. Oracle, Oracle Fusion Cloud, SAP, Coupa, Workday,
"the ERP", "the S2C platform", "Merlin". NEVER put a product / system / vendor name where a
person belongs, and never turn one into an action's named individual ("ask Oracle Cloud
Fusion to…" is invalid). If you have no roster-verified person, use a ROLE instead ("the
procurement lead", "their IT director") — never invent a person and never repurpose a
product as one. Records that name an unverified person are REJECTED and re-run."""


def latest(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "is.true",
                                "select": "version,content"},
                        headers=H, verify=False, timeout=60).json()
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    return rows[0]


def bump(v):
    a, b = v.split("."); return f"{a}.{int(b) + 1}"


plan = []
for eng in ["extract", "sweep"]:
    cur = latest(eng); content = cur["content"]
    if MARKER in content:
        print(f"  SKIP {eng}: already has the rule"); continue
    if ANCHOR in content:
        new = content.replace(ANCHOR, ANCHOR + BLOCK, 1)
    else:  # fallback: after the first line
        nl = content.find("\n"); new = content[:nl + 1] + BLOCK + content[nl + 1:]
    nv = bump(cur["version"])
    plan.append((eng, cur["version"], nv, new))
    print(f"  {eng:8} v{cur['version']} -> v{nv}  (+{len(new) - len(content):,} chars, anchored={'yes' if ANCHOR in content else 'FALLBACK'})")

if not plan:
    print("nothing to do."); raise SystemExit(0)
if not APPLY:
    print("\nDRY RUN — re-run with --apply to lock."); raise SystemExit(0)

NOTE = ("Added PEOPLE vs PRODUCTS hard rule: product/system/vendor names (Oracle Fusion "
        "Cloud, SAP, Coupa, 'the ERP', Merlin) are NOT people and must never fill a "
        "person/owner/stakeholder slot or a to-do target; use a role if no verified person. "
        "Stops the recurring 'Oracle Cloud Fusion' fabrication.")
for eng, ov, nv, new in plan:
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json={"engine": eng, "version": nv, "content": new, "kind": "minor",
                            "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                      verify=False, timeout=90)
    print(f"lock {eng} v{nv} -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:160]}")
print("DONE")
