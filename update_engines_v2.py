"""Omnivision Studio — lock NEW MINOR versions of the sweep + win engines (source of truth).

Edits are SURGICAL string-replacements on the CURRENT locked content, each guarded by an
assert so a missing anchor aborts (never ships a mangled prompt). Dry-run by default; --apply
inserts+locks the new versions and unlocks the priors.

  sweep 10.0 -> 10.1 :
    - strip the dead AIS_Score__c / AIS_Status__c / AIS_Why__c fields from Q2 (they don't
      exist on Opportunity in this org; a SELECT that includes them 400s the WHOLE query,
      which is what silently starved the sweep of SFDC next-step / activity ground truth).
    - reinforce: an RSD-logged Next Step / email / call (SFDC Task/Event) is first-class
      buyer-engagement evidence EVEN when Avoma was never permitted (no transcript, no note).
  win 10.3 -> 10.4 :
    - add 4.4a QUALIFICATION-DEPTH FLOOR: hard-confirmed EB engagement + MEDDPICC depth on a
      Formal-Eval+ deal softens the dark-days decay and holds a modest win floor (so a deeply
      qualified but currently-dark deal like ACEN doesn't crater to a cold-deal score).
"""
import sys, os, json, warnings
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


def latest_locked(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "eq.true",
                                "select": "id,version,content"},
                        headers=H, verify=VERIFY, timeout=30).json()

    def vkey(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: vkey(r["version"]), reverse=True)
    return rows[0] if rows else None


def replace_once(text, old, new, label):
    n = text.count(old)
    assert n == 1, f"[{label}] anchor found {n} times (need exactly 1): {old[:60]!r}"
    return text.replace(old, new, 1)


def insert_after(text, anchor, addition, label):
    i = text.find(anchor)
    assert i != -1, f"[{label}] anchor NOT found: {anchor[:60]!r}"
    assert text.count(anchor) == 1, f"[{label}] anchor not unique"
    j = i + len(anchor)
    return text[:j] + addition + text[j:]


# ------------------------------------------------------------------ SWEEP 10.0 -> 10.1
sw = latest_locked("sweep")
assert sw and sw["version"] == "10.0", f"unexpected locked sweep: {sw and sw['version']}"
s = sw["content"]
before_ais = s.count("AIS_Score__c")
# 1) remove the three dead fields from the Q2 backtick list
s = replace_once(s, "AIS_Score__c, AIS_Status__c, AIS_Why__c, ", "", "sweep.Q2-list")
# 2) replace the "Do not normalise AIS..." instruction with a hard do-not-query note
s = replace_once(
    s,
    "Do not normalise AIS; interpret the score through `AIS_Status__c`. ",
    "The AIS fields (AI Score / AI Status / AI Why) are NOT present on Opportunity in this "
    "org; never include an AIS column in any SELECT (it 400s the entire query). ",
    "sweep.Q2-note")
# 3) reinforce RSD-logged activity as evidence when Avoma was not permitted
s = insert_after(
    s, "never manufacture what was said.",
    " When Avoma was never permitted in a call there is no transcript AND no "
    "`-- Avoma Note Start --` summary, yet the call or email still exists as a logged SFDC "
    "Activity: a `Task` or `Event`, or a dated entry the RSD wrote into `Next_Step__c` / "
    "`Next_Step_History__c`. That logged activity, even with ZERO Avoma content, is "
    "first-class buyer-engagement evidence. Count it, date it, attribute it by role, and "
    "NEVER read the deal as dark or 'no data' when Next Steps or logged emails/calls show "
    "recent movement. The RSDs keep next steps and activities current in Salesforce; treat "
    "that as ground truth whenever Avoma is silent.",
    "sweep.rsd-activity")
assert s.count("AIS_Score__c") == 0, f"AIS_Score__c still present in sweep ({s.count('AIS_Score__c')})"
assert "first-class buyer-engagement evidence" in s
SWEEP_NEW = s
print(f"sweep 10.0 -> 10.1: {len(sw['content'])} -> {len(SWEEP_NEW)} chars | "
      f"AIS mentions {before_ais} -> {SWEEP_NEW.count('AIS_')} ")

# ------------------------------------------------------------------ WIN 10.3 -> 10.4
wn = latest_locked("win")
assert wn and wn["version"] == "10.3", f"unexpected locked win: {wn and wn['version']}"
w = wn["content"]
QDEPTH = (
    "\n\n4.4a QUALIFICATION-DEPTH FLOOR (historical-depth credit). Win Position asks 'can we "
    "win it IF it re-engages', so a deal that was GENUINELY deep does not collapse to a "
    "cold-deal score merely because it is dark right now. When HARD-confirmed depth exists "
    "(the economic buyer had DIRECT Zycus face time that demonstrably happened, AND at least "
    "3 MEDDPICC pillars among EB / decision process / decision criteria / pain are confirmed "
    "from real buyer events, AND the deal reached Formal Evaluation or later), soften the "
    "staleness decay on those fundamentals (floor the >180d multiplier at x0.3 rather than "
    "x0.1) and hold a Win FLOOR of ~35 (~40 at Vendor Selected or later). This credits a "
    "real, re-winnable position. It NEVER breaches a section 5 ceiling, does NOT apply to "
    "rep-claimed depth / a single demo / a buyer-voiced loss, and lives in Win ONLY "
    "(Momentum still reflects that the deal is dark now). Absent this hard depth, the "
    "standard decay in 4.4 applies and a genuinely cold or shallow dark deal still scores low.")
w = insert_after(w, "(Process-mode uses the process clock.)", QDEPTH, "win.4.4a")
w = w.rstrip() + (
    "\n\n## 8b. Qualification-depth acceptance\n"
    "Vendor Selected 300d+ dark + exco postponed, BUT EB directly engaged and MEDDPICC deep "
    "-> ~38-46 (qualification-depth floor, 4.4a). The SAME deal with only shallow or "
    "rep-claimed depth -> ~22-28. In both cases Momentum stays low because the deal is dark now.")
assert "QUALIFICATION-DEPTH FLOOR" in w and "## 8b." in w
WIN_NEW = w
print(f"win 10.3 -> 10.4: {len(wn['content'])} -> {len(WIN_NEW)} chars")


def insert_lock(engine, new_version, prior_id, kind, note, content):
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json=[{"engine": engine, "version": new_version, "kind": kind, "note": note,
                             "content": content, "locked": True, "locked_by": "omnivision-calib-2026-07-09"}],
                      verify=VERIFY, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"insert {engine} v{new_version} FAILED {r.status_code}: {r.text[:300]}")
    # unlock the prior so there is exactly one locked version per engine
    u = requests.patch(f"{BASE}/rest/v1/scoring_instructions",
                       params={"id": f"eq.{prior_id}"},
                       headers={**H, "Content-Type": "application/json"},
                       json={"locked": False}, verify=VERIFY, timeout=30)
    print(f"  {engine} v{new_version}: inserted+locked ({len(content)} chars); prior id {prior_id} unlock={u.status_code}")


if not APPLY:
    print("\n[DRY-RUN] anchors OK, edits composed. Re-run with --apply to lock.")
    # show the exact new clauses for review
    print("\n--- sweep RSD clause preview ---")
    k = SWEEP_NEW.find("When Avoma was never permitted")
    print("  " + SWEEP_NEW[k:k+300])
    print("\n--- win 4.4a preview ---")
    print("  " + QDEPTH.strip()[:400])
    raise SystemExit(0)

print("\n=== APPLY ===")
insert_lock("sweep", "10.1", sw["id"], "minor",
            "Strip dead AIS_Score__c/Status/Why from Q2 (INVALID_FIELD 400 starved the SFDC "
            "next-step/activity read); reinforce that RSD-logged next-steps/emails/calls are "
            "first-class engagement evidence even when Avoma was not permitted (no transcript).",
            SWEEP_NEW)
insert_lock("win", "10.4", wn["id"], "minor",
            "Add 4.4a Qualification-Depth Floor: hard-confirmed EB direct engagement + MEDDPICC "
            "depth on a Formal-Eval+ deal softens dark-days decay and holds a modest win floor "
            "(~35, ~40 at Vendor Selected+), so a deeply-qualified but currently-dark deal is "
            "scored as re-winnable, not cold. Momentum unaffected. Never breaches a ceiling.",
            WIN_NEW)
# stamp locked_at for any freshly-locked rows missing it
requests.patch(f"{BASE}/rest/v1/scoring_instructions",
               params={"locked": "eq.true", "locked_at": "is.null"},
               headers={**H, "Content-Type": "application/json"},
               json={"locked_at": "now()"}, verify=VERIFY, timeout=30)
print("\nDONE — sweep 10.1 + win 10.4 locked.")
