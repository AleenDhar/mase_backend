"""Omnivision Studio — REASON HEADLINE FORMAT for win + mom (match the ACEN card): every driver
bullet must lead with a short bold headline, then ' — ', then the specific evidence. This makes
the CRO panel render scannable titled bullets (the UI bolds the text before ' — '), instead of a
wall of detail. Surgical insert into section 7; dry-run by default, --apply to lock.

  win 10.5 -> 10.6   mom 10.6 -> 10.7
"""
import sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret()
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

FORMAT = (
    "\n\n**Reason format — EVERY driver bullet (this is how the card renders):** lead with a "
    "short 3–6 word plain-language HEADLINE, then a space–em-dash–space ( — ), then the "
    "specific evidence (a date, a name, a verbatim quote, a dollar figure). The headline is the "
    "scannable claim; the evidence proves it. Examples: `Incumbent actively blocking — SAP "
    "Ariba's live beta is the explicit gate on the award; champion silent since 26 May 2026.` / "
    "`Economic buyer never engaged — the Casey McDowell exec session has slipped since Feb 2026 "
    "and still hasn't happened.` Never a bare label, and never a wall of text with no headline.\n")

NEXT = {"win": ("10.5", "10.6"), "mom": ("10.6", "10.7")}


def latest_locked(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "eq.true",
                                "select": "id,version,content"},
                        headers=H, verify=VERIFY, timeout=30).json()

    def vk(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: vk(r["version"]), reverse=True)
    return rows[0] if rows else None


plan = []
for eng, (cur_v, new_v) in NEXT.items():
    row = latest_locked(eng)
    assert row and row["version"] == cur_v, f"{eng}: expected locked v{cur_v}, got {row and row['version']}"
    c = row["content"]
    assert c.count("## 7. Output") == 1, f"{eng}: '## 7. Output' anchor not unique"
    assert "Reason format — EVERY driver bullet" not in c, f"{eng}: format already present"
    i = c.find("## 7. Output")
    j = c.find("\n", i) + 1                      # end of the heading line
    new_c = c[:j] + FORMAT + c[j:]
    plan.append((eng, row["id"], cur_v, new_v, new_c))
    print(f"{eng} v{cur_v} -> v{new_v}: {len(c)} -> {len(new_c)} chars")

if not APPLY:
    print("\n[DRY-RUN] --apply to lock.\n--- inserted block ---\n" + FORMAT.strip())
    raise SystemExit(0)

print("\n=== APPLY ===")
for eng, prior_id, cur_v, new_v, new_c in plan:
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json=[{"engine": eng, "version": new_v, "kind": "minor",
                             "note": "Reason headline format: every driver bullet leads with a short "
                                     "headline then ' — ' then evidence, so the CRO card renders titled "
                                     "bullets (ACEN-style) instead of a wall of detail.",
                             "content": new_c, "locked": True, "locked_by": "omnivision-reasonfmt-2026-07-09"}],
                      verify=VERIFY, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"insert {eng} v{new_v} FAILED {r.status_code}: {r.text[:300]}")
    u = requests.patch(f"{BASE}/rest/v1/scoring_instructions", params={"id": f"eq.{prior_id}"},
                       headers={**H, "Content-Type": "application/json"}, json={"locked": False},
                       verify=VERIFY, timeout=30)
    print(f"  {eng} v{new_v} locked; prior v{cur_v} unlock={u.status_code}")
requests.patch(f"{BASE}/rest/v1/scoring_instructions", params={"locked": "eq.true", "locked_at": "is.null"},
               headers={**H, "Content-Type": "application/json"}, json={"locked_at": "now()"}, verify=VERIFY, timeout=30)
print("\nDONE — win 10.6 + mom 10.7 locked (reason headline format).")
