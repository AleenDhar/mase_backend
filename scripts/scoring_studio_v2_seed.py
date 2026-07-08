"""Scoring Version Studio v2 — schema extension + seed for the 3 NEW assets
(per the governance prototype landed in the MASE repo, scoring-studio/index.html,
commit 4dda444 'Add MASE Scoring Version Studio (governance prototype)').

WHAT IT DOES (idempotent; --apply to write, else dry-run):
1. DDL: extends the `scoring_instructions.engine` CHECK constraint from the five
   original engines to the EIGHT Studio v2 assets:
      extract · win · mom · todo · sum · sweep · vendordict · playbook
2. Seeds (locked):
   - sweep      v10.0  <- prompts/studio_seeds/deal-sweep-v3.md   (Deal Sweep / Deal Drawer —
                          the canonical-record BASE prompt, replaces the monolithic v2)
   - vendordict v1.0   <- prompts/studio_seeds/vendor-dictionary.json (reference asset,
                          cited as {{ref:vendor-dictionary}})
   - playbook   v1.0   <- prompts/studio_seeds/deal-playbook.md   (reference asset,
                          cited as {{ref:deal-playbook}})
   - extract    v10.4  <- the CURRENT locked extract content + §A5b (vendor/competitor
                          entity resolution, prompts/studio_seeds/extract-a5b-vendor.md)
                          inserted before '## A6.' — exactly the prototype's composition.
Skips any (engine, version) that already exists. DDL via the Supabase Management API;
rows via PostgREST."""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS = os.path.join(ROOT, "prompts", "studio_seeds")

sec = load_secret()
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"
TOK = sec["SUPABASE_ACCESS_TOKEN"]


def mgmt(q):
    r = requests.post(MGMT, headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
                      json={"query": q}, verify=VERIFY, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"MGMT SQL failed ({r.status_code}): {r.text[:300]}\nquery: {q[:200]}")
    return r.json()


def read_seed(name):
    p = os.path.join(SEEDS, name)
    txt = open(p, encoding="utf-8").read().strip()
    # deal-sweep-v3.md carries a stray trailing code fence — strip a lone ``` at EOF
    if txt.endswith("```") and txt.count("```") % 2 == 1:
        txt = txt[:-3].rstrip()
    return txt


def exists(engine, version):
    r = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                     params={"engine": f"eq.{engine}", "version": f"eq.{version}", "select": "id", "limit": "1"},
                     headers=H, verify=VERIFY, timeout=30).json()
    return bool(isinstance(r, list) and r)


def latest_locked(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "eq.true",
                                "select": "version,content"},
                        headers=H, verify=VERIFY, timeout=30).json()
    def vkey(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: vkey(r["version"]), reverse=True)
    return rows[0] if rows else None


def insert(engine, version, kind, note, content):
    if exists(engine, version):
        print(f"  {engine} v{version}: already present — skip")
        return False
    if not APPLY:
        print(f"  [DRY] would insert {engine} v{version} (locked, {len(content)} chars)")
        return False
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json=[{"engine": engine, "version": version, "kind": kind, "note": note,
                             "content": content, "locked": True, "locked_by": "studio-v2-seed"}],
                      verify=VERIFY, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"insert {engine} v{version} FAILED {r.status_code}: {r.text[:300]}")
    print(f"  {engine} v{version}: inserted + locked ({len(content)} chars)")
    return True


# ---------------------------------------------------------------- 1. DDL: extend the CHECK
print("=== 1. engine CHECK constraint ===")
cons = mgmt("select conname, pg_get_constraintdef(oid) as def from pg_constraint "
            "where conrelid = 'scoring_instructions'::regclass and contype = 'c'")
target = None
for c in cons:
    if "engine" in (c.get("def") or ""):
        target = c
        break
print(f"current: {target}")
WANT = "'extract','win','mom','todo','sum','sweep','vendordict','playbook'"
if target and all(k in (target.get("def") or "") for k in ("sweep", "vendordict", "playbook")):
    print("constraint already covers the 8 assets — skip DDL")
elif not APPLY:
    print(f"[DRY] would replace {target['conname'] if target else '(none)'} with CHECK (engine in ({WANT}))")
else:
    if target:
        mgmt(f"alter table scoring_instructions drop constraint {target['conname']}")
    mgmt(f"alter table scoring_instructions add constraint scoring_instructions_engine_check "
         f"check (engine in ({WANT}))")
    print(f"constraint replaced: engine in ({WANT})")

# ---------------------------------------------------------------- 2. seeds
print("\n=== 2. seed the new assets ===")
sweep_txt = read_seed("deal-sweep-v3.md")
vendor_txt = read_seed("vendor-dictionary.json")
json.loads(vendor_txt)   # sanity: the dictionary must stay valid JSON
playbook_txt = read_seed("deal-playbook.md")
a5b = read_seed("extract-a5b-vendor.md")

insert("sweep", "10.0", "initial",
       "Initial release — Deal Sweep (Deal Drawer) v3 rebuild: top-down precedence, primitives "
       "inherited from the locked Signal Extraction engine, ONE recency model, safety-net-as-plan, "
       "standalone verdict dropped in favour of ai.forecast_read. This locked asset IS the sweep's "
       "base system prompt (replaces the monolithic mase_deal_sweep v2).", sweep_txt)
insert("vendordict", "1.0", "initial",
       "Initial release — canonical vendor entity-resolution dictionary (canonical name + aliases + "
       "category + role + collision guards + terminology normalization). Cited by engines as "
       "{{ref:vendor-dictionary}}; corrected HERE (locked), never in-prompt.", vendor_txt)
insert("playbook", "1.0", "initial",
       "Initial release — Zycus Deal-Progression Playbook: the single domain-knowledge reference "
       "(sales motion, stage→milestone map, engagement-depth ladder, MEDDPICC backbone, contracting "
       "relay) shared by the sweep, the Studio engines, and the chat/briefing agents. Cited as "
       "{{ref:deal-playbook}}.", playbook_txt)

# extract v10.4 = current locked extract + §A5b before '## A6.'
cur = latest_locked("extract")
print(f"\ncurrent locked extract: v{cur['version'] if cur else '?'}")
if cur and not exists("extract", "10.4"):
    if "## A5b." in (cur.get("content") or ""):
        print("  extract already carries §A5b — skip")
    elif "## A6." not in (cur.get("content") or ""):
        print("  !! '## A6.' anchor not found in the locked extract — NOT composing v10.4 (manual review)")
    else:
        v104 = cur["content"].replace("## A6.", a5b + "\n\n## A6.", 1)
        insert("extract", "10.4", "minor",
               "§A5b VENDOR / COMPETITOR ENTITY RESOLUTION — the §A5 people-resolution discipline "
               "extended to company names. Every competitor / vendor / incumbent / ERP mention is "
               "normalized then resolved against the canonical MASE Vendor Dictionary (cited as "
               "{{ref:vendor-dictionary}}): exact-alias → fuzzy (token_set_ratio ≥ 88 / Levenshtein "
               "≤ 2) → canonical name + category + role, deduped, with the dictionary's collision "
               "guards, terminology normalization, and a Zycus self-guard. Kills the "
               "'Tonkin/Tronkeon → three competitors' fragmentation.", v104)
else:
    print("  extract v10.4 already present — skip")

if APPLY:
    out = mgmt("update scoring_instructions set locked_at = now() "
               "where locked and locked_at is null returning engine, version")
    print(f"\nlocked_at stamped: {out}")
print("\nDONE" + ("" if APPLY else " (dry-run — pass --apply to write)"))
