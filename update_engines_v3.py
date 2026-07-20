"""Omnivision Studio — wire the two locked REFERENCE assets ({{ref:vendor-dictionary}} +
{{ref:deal-playbook}}) into every engine that should apply them, as a concise appended
'## References' footer. Additive only (no existing logic touched). Dry-run by default; --apply
inserts+locks the new minor versions and unlocks the priors.

  extract 10.4 -> 10.5   win 10.4 -> 10.5   mom 10.5 -> 10.6
  todo    10.1 -> 10.2   sum 10.1 -> 10.2   sweep 10.1 -> 10.2

During a SWEEP these tokens resolve to pointers and the full reference bodies are appended once
(deal_engine_sweep._studio_block). The standalone AI scorer renders win/mom tokens as plain
names (deal_engine_ai_scoring._prompt), so no raw token leaks there.
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

FOOTER = "\n\n## References (locked assets, appended in full on every sweep)\n"
CITE = {
    "extract": (FOOTER + "Resolve every vendor / competitor / incumbent / ERP name against "
                "{{ref:vendor-dictionary}} before it enters a signal. Interpret stage, milestone "
                "and MEDDPICC signals against the stage->milestone map and MEDDPICC backbone in "
                "{{ref:deal-playbook}}."),
    "win": (FOOTER + "Use {{ref:deal-playbook}} for the stage->milestone map, MEDDPICC backbone "
            "and engagement-depth ladder that calibrate the stage anchor (4.1) and rubric (4.2). "
            "Render every competitor name via {{ref:vendor-dictionary}}."),
    "mom": (FOOTER + "Use {{ref:deal-playbook}} for the engagement-depth ladder and the stage / "
            "process milestones behind sections 4-5. Render every competitor name via "
            "{{ref:vendor-dictionary}}."),
    "todo": (FOOTER + "Ground every recommended move in the stage->next-best-action motion and "
             "the contracting relay in {{ref:deal-playbook}}. Name competitors via "
             "{{ref:vendor-dictionary}}."),
    "sum": (FOOTER + "Frame 'what matters now' for the deal's current stage using "
            "{{ref:deal-playbook}}. Name competitors via {{ref:vendor-dictionary}}."),
    "sweep": (FOOTER + "The two locked reference assets are appended to this prompt on every "
              "sweep run. Resolve ALL vendor / competitor / incumbent names via "
              "{{ref:vendor-dictionary}} (see 4.3b / 6). Apply {{ref:deal-playbook}} for the "
              "stage->milestone map, MEDDPICC backbone, engagement-depth ladder and the "
              "Vendor-Selected->PO contracting relay (see 12)."),
}
NEXT = {"extract": ("10.4", "10.5"), "win": ("10.4", "10.5"), "mom": ("10.5", "10.6"),
        "todo": ("10.1", "10.2"), "sum": ("10.1", "10.2"), "sweep": ("10.1", "10.2")}


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
    content = row["content"]
    assert "## References (locked assets" not in content, f"{eng}: References footer already present"
    new_content = content.rstrip() + CITE[eng]
    # sanity: the tokens we intend are present after edit
    assert "{{ref:" in new_content
    plan.append((eng, row["id"], cur_v, new_v, content, new_content))
    print(f"{eng:8} v{cur_v} -> v{new_v}: {len(content)} -> {len(new_content)} chars "
          f"(+{len(new_content)-len(content)})")

if not APPLY:
    print("\n[DRY-RUN] footers composed; re-run with --apply to lock.")
    print("\n--- win footer preview ---")
    print(CITE["win"].strip())
    raise SystemExit(0)

print("\n=== APPLY ===")
for eng, prior_id, cur_v, new_v, _old, new_content in plan:
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json=[{"engine": eng, "version": new_v, "kind": "minor",
                             "note": f"Wire locked reference assets: cite {{ref:vendor-dictionary}} "
                                     f"and/or {{ref:deal-playbook}} at this engine's decision points "
                                     f"(References footer). Additive; no logic change.",
                             "content": new_content, "locked": True,
                             "locked_by": "omnivision-refs-2026-07-09"}],
                      verify=VERIFY, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"insert {eng} v{new_v} FAILED {r.status_code}: {r.text[:300]}")
    u = requests.patch(f"{BASE}/rest/v1/scoring_instructions", params={"id": f"eq.{prior_id}"},
                       headers={**H, "Content-Type": "application/json"},
                       json={"locked": False}, verify=VERIFY, timeout=30)
    print(f"  {eng:8} v{new_v} locked; prior v{cur_v} (id {prior_id}) unlock={u.status_code}")

requests.patch(f"{BASE}/rest/v1/scoring_instructions",
               params={"locked": "eq.true", "locked_at": "is.null"},
               headers={**H, "Content-Type": "application/json"},
               json={"locked_at": "now()"}, verify=VERIFY, timeout=30)
print("\nDONE — all 6 engines now cite the reference assets.")
