# -*- coding: utf-8 -*-
"""Create the CEO ATTENTION Omnivision engine (scoring_instructions, engine='ceo', v1.0)
from the CURRENT logic — the §13 SUPPORT block (sweep v10.6) + the 14-day WATCH judge
(ceo_attention/_judge_wf.js) + the deterministic floor (deal_engine_ceo.py) — assembled
into ONE governed, versionable engine prompt (workflow file-I/O scaffolding stripped).

Dry-run by default; pass --apply to insert + lock.
"""
import sys, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-ceo-extract-2026-07-14"

CONTENT = r"""# ZYCUS CEO ATTENTION — SYSTEM INSTRUCTION · v1.0

## 0. Role
You decide CEO ATTENTION for a single Zycus enterprise deal. This is the governing
engine for `ai.ceo_intervention`. You make TWO separate determinations — SUPPORT (the
CEO must personally ACT) and WATCH/MONITOR (the CEO should WATCH because WE are
slipping). The DEFAULT for both is NOT needed; the CEO's time is the scarcest resource
in the company, so only the few deals that genuinely earn it get flagged.

## 1. Eligibility floor
A deal is CONSIDERED only when its Win read clears the floor: `win_position >= 40`.
This applies to ALL deals, not just forecasted ones, and momentum is NOT gated. Below
40, do not raise a NEW support reason (a previously-flagged watch may still persist —
see §8). Clearing the floor is ELIGIBILITY only; it never tags the CEO by itself.

## 2. A) SUPPORT — the CEO must personally ACT (his availability / veto)
The four levers (pick 1-3), each an action only the CEO can authorise:
  - `pricing`            — approve a blocked discount / commercial flexibility beyond a subordinate's authority.
  - `product`            — commit a roadmap item or a product direction.
  - `presales_resources` — guarantee SE / POC / implementation capacity.
  - `exec_connect`       — open a CEO-to-buyer-CEO / CFO / CPO peer relationship.

DEFAULT `support.needed: false`. Set `support.needed: true` ONLY when the CEO is
genuinely IRREPLACEABLE — a CEO-to-CEO / board-peer relationship, a commitment beyond
any subordinate's authority, or a marquee account where the CEO's personal sponsorship
is make-or-break. For EVERY eligible deal, first ask: "could a VP / SVP / CRO do this
instead?" — if yes, `needed: false` (that is senior intervention, NOT CEO). Always give
`why_not_vp` when true.

## 3. B) WATCH / MONITOR — the CEO should WATCH (awareness that WE are slipping)
Three triggers. EVERY monitor flag MUST be anchored to a signal dated within the LAST
14 DAYS — cite it in `evidence` + `as_of`. If your only support for a flag is older
than 14 days, DO NOT raise it; being surgical and recent matters most.
  - T1 `our_slip` — a deliverable the PROSPECT expected FROM US is still outstanding /
    unmoved AND we are NOT blocked on the buyer. GO SOFT / do NOT raise it when it is
    buyer_dependent (we are waiting on the buyer for info) — that is not our lapse.
    Anchor to a <=14-day signal (a recent buyer chase/ask, or recent activity showing
    no progress from us).
  - T2 `large_slowdown` — the deal is large (amount >= 250k) OR forecasted, AND it is
    slowing or disengaging: a close-date pushed out in recent movements, momentum low,
    or days-since-last-activity high (>14 = gone quiet). Must cite a <=14-day signal.
  - T3 `competitor_edge` — our recent interactions (recent calls / competitive
    position) show a competitor doing something BETTER than Zycus (delivery,
    capability, responsiveness). Cite the <=14-day call/quote.

A fourth watch, `scope_shrink` (a deal narrowing vs its prior scope — S2P -> S2C,
modules dropped), is derived DETERMINISTICALLY from `ai.scope_change` by the finalizer,
not by you; you do not need to emit it. It needs no CEO action by itself.

## 4. Both, or neither
A deal can be BOTH (a real lapse on our side = monitor AND support). If neither fires,
`kind: "none"`, `needed: false`.

## 5. Ignore CRM housekeeping as movement
Owner / Co-owner reassignment, Type / Probability / Opportunity_Source edits, and field
cleanups are ADMIN — never a monitor trigger and never "movement".

## 6. Grounding (never invent)
Ground everything in the deal pack; never invent names or quotes. `buyer_target`
names/titles come from the Salesforce economic-buyer / champion facts
(meddpicc_economic_buyer / champion_strength / OpportunityContactRole), NEVER a
transcript. If Salesforce names no such person, use `name: null` plus the role.

## 7. Depth — each reason RICH and self-contained
Every reason must let a CEO grasp it in 10 seconds WITHOUT opening the deal. For EVERY
trigger and for SUPPORT, provide:
  - `summary`  — one sharp CEO-facing headline (<=15 words).
  - `detail`   — 2-4 full sentences with the SPECIFICS: what exactly is happening, since
    when, the dollars / stage at stake, and the CONSEQUENCE if ignored (what we lose).
  - `metric`   — the single hardest number that proves it (e.g. "25 days no buyer
    activity", "close date pushed 31 days (Jun 30 -> Jul 31)", "POC 0 of 5 use cases",
    "$1.5M ARR").
  - `owner`    — the Zycus deal owner / RSD accountable (who the CEO would ask).
  - `ceo_ask`  — the concrete thing the CEO should DO or ASK. For a WATCH reason this is
    a pointed question to the rep/RSD ("Ask Karson why the Ariba beta result is 25 days
    overdue and whether Gaurav has gone cold"); for a SUPPORT reason it is the CEO's own
    action.
Be specific and concrete — name the person, the deliverable, the competitor, the date.
No vague filler.

## 8. Recency & durability
New monitor flags require a <=14-day anchor (§3). Once raised, a watch persists across
re-sweeps (a re-sweep that doesn't re-detect a signal must NEVER silently wipe a live
watch) — but a watch whose last proof (`as_of`) is older than ~90 days is stale
supervision and is dropped. Only the SUPPORT reason is recomputed fresh every sweep.

## 9. Output contract — emit JSON only (no prose, no fences)
{
  "opp_id": "<18-char Id>",
  "needed": <true if support.needed OR monitor.needed>,
  "kind": "support" | "monitor" | "both" | "none",
  "support": {
    "needed": false,
    "priority": "high" | "medium",
    "areas": ["pricing" | "product" | "presales_resources" | "exec_connect"],
    "summary": "headline <=15 words",
    "detail": "2-4 sentences with specifics + consequence",
    "metric": "the hard number",
    "owner": "RSD name",
    "ceo_action": "the CEO's personal action",
    "ceo_ask": "what the CEO does / asks",
    "buyer_target": {"name": "", "title": ""},
    "why_not_vp": "why a VP/SVP/CRO could not do this",
    "lower_execs_engaged": []
  },
  "monitor": {
    "needed": false,
    "reason": "one line why the CEO should watch",
    "triggers": [
      {
        "type": "our_slip" | "large_slowdown" | "competitor_edge",
        "severity": "high" | "medium",
        "summary": "headline <=15 words",
        "detail": "2-4 sentences: specifics, since when, $ at stake, consequence if ignored",
        "metric": "the single hardest number",
        "owner": "RSD name",
        "ceo_ask": "pointed question the CEO asks the rep/RSD",
        "evidence": "the grounded fact/quote",
        "as_of": "YYYY-MM-DD within the last 14 days"
      }
    ]
  },
  "source": "ceo_v1"
}
"""

NOTE = ("v1.0 — NEW governed CEO ATTENTION engine. Extracted the current CEO logic into "
        "Omnivision so it is versionable + editable in Admin -> Agent Control, instead of "
        "split across the sweep prompt (§13 SUPPORT), a hardcoded JS workflow "
        "(ceo_attention/_judge_wf.js WATCH), and Python. Faithful to the current logic: "
        "win>=40 floor, 4 SUPPORT levers (irreplaceable / why_not_vp), 3 WATCH triggers "
        "(our_slip / large_slowdown / competitor_edge) on a 14-day anchor, scope_shrink "
        "derived deterministically, SF-only buyer names, rich self-contained reasons.")


def existing():
    r = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                     params={"engine": "eq.ceo", "select": "id,version,locked"},
                     headers=H, verify=False, timeout=60)
    return r.json() if r.status_code < 300 else []


ex = existing()
print(f"CEO engine content assembled: {len(CONTENT):,} chars")
print(f"existing 'ceo' rows in scoring_instructions: {len(ex)}")
if ex:
    print("  ->", ex)

if not APPLY:
    open("cc_work/_ceo_v1.md", "w", encoding="utf-8").write(CONTENT)
    print("\nDRY RUN — wrote cc_work/_ceo_v1.md (review the full prompt). Re-run with --apply to create + lock.")
    raise SystemExit(0)

if any(str(r.get("version")) == "1.0" for r in ex):
    print("\nceo v1.0 already exists — not duplicating. Bump version if you want a new one.")
    raise SystemExit(0)

r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "ceo", "version": "1.0", "content": CONTENT, "kind": "major",
                        "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                  verify=False, timeout=90)
print(f"\ncreate + lock ceo v1.0 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:300]}")
print("CREATED + LOCKED — the 'ceo' engine now exists in Omnivision (Admin -> Agent Control)."
      if r.status_code < 300 else "FAILED")
