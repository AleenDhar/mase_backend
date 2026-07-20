"""Omnivision Studio — WIN 10.7 -> 10.8 and MOM 10.7 -> 10.8. User-approved 2026-07-09.

WHY (Robert Bosch, evidence-backed):
  * mom §5 process-mode never fired because its on-track test read "last deliverable on time"
    as OUR deliverable — our Swagger docs were 6 weeks late — so a live RFP buyer looked stalled.
  * Both engines measured "dark" off MEETINGS. Bosch's buyer ISSUED RFP round-2 documents on
    29 Jun (10 days before the sweep). The engines called it "44 days dark".
  * win §4.4a's qualification-depth floor was GATED on EB direct face time + 3 of 4 named
    MEDDPICC pillars. Bosch has a 17-user POC, a buyer-run scorecard, 14 buyer attendees across
    three Bosch entities — and no CPO call — so a genuinely deep position scored as a cold one.
  * Nothing scored the compound buy-signal: amount $300K->$1.2M + Pipeline->Best Case +
    Formal Eval->Shortlisted, all within ~70 days.
  * Neither engine had a frequency/trajectory factor.

GUARDS PRESERVED (deliberately): win §5.1 stage ceiling still binds; depth without an economic
buyer cannot exceed 48; win §4.4b momentum gate still supersedes depth when momentum < 30 AND
process-mode is off — so ACEN (Vendor Selected, momentum 8, 330d dark, no live RFP) still halves
to ~20. Depth must never rescue a genuinely dead deal.

Dry-run by default; pass --apply to write + lock.
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
BY = "omnivision-depth-and-process-2026-07-09"

# ---------------------------------------------------------------- WIN edits
W_431_OLD = ("4.3 CRM TREND NUDGE (± ~8). Stage +/−; forecast upgrade (→Best Case→Commit) = strong "
             "signal, extra +4; downgrade −; amount +/−; close pulled-in +/pushed −. Recency-weight.")
W_431_NEW = (
    "4.3 CRM TREND NUDGE (± ~12). Stage +/−; forecast upgrade (→Best Case→Commit) = strong signal, "
    "extra +4; downgrade −; amount +/−; close pulled-in +/pushed −. Recency-weight. "
    "DEAL-STRENGTHENING COMPOUND (added v10.8): when the amount was RAISED **and** the forecast "
    "category was upgraded **and** the stage advanced, all inside ~90 days, that is ONE compound "
    "buy-signal, not three small nudges — score it +10…+12 and name it, e.g. 'Deal strengthening — "
    "$300K→$1.2M, Pipeline→Best Case, Formal Eval→Shortlisted, all inside 70 days.' A single move "
    "on its own stays within ±4. The buyer's own procurement raising our ceiling is the strongest "
    "CRM signal there is; do not dilute it.")

W_44_OLD = "4.4 RECENCY & STALENESS DECAY. Age-discount each factor by age of last REAL event:"
W_44_NEW = (
    "4.4 RECENCY & STALENESS DECAY. Age-discount each factor by the age of the last BUYER ACTION — "
    "a meeting, a buyer reply, a buyer-ISSUED document or deadline (new RFP round, BAFO invitation, "
    "finalist notice, scoring request), or a buyer clarification round. A buyer-issued document or "
    "deadline RESETS the clock exactly as a meeting does; rep chasing NEVER does. (v10.8: this read "
    "'age of last REAL event' and was applied to meetings only — an actively-engaging RFP buyer who "
    "had just issued round-2 documents was scored as 44 days dark.) Age-discount:")

W_44A_HEAD = "4.4a QUALIFICATION-DEPTH FLOOR"
W_44A_TAIL = "and a genuinely cold or shallow dark deal still scores low."
W_44A_NEW = (
    "4.4a QUALIFICATION-DEPTH FLOOR — measured, not gated (rewritten v10.8). Win Position asks 'can "
    "we win it IF it re-engages', so a deal we qualified DEEPLY does not collapse to a cold-deal "
    "score merely because it is quiet right now. Compute a QUALIFICATION DEPTH INDEX (QDI) from the "
    "whole record, ALL-TIME, with no recency discount:\n"
    "  • POC / sandbox actually executed with buyer users .................. 3\n"
    "  • buyer ran its OWN formal evaluation / scored us ................... 3\n"
    "  • technical deep-dive / InfoSec / integration session ............... 2\n"
    "  • workshop or multi-day onsite ..................................... 2\n"
    "  • ≥8 distinct buyer attendees (≥4 → 1) ............................. 2\n"
    "  • ≥10 transcribed buyer sessions (≥5 → 1) .......................... 2\n"
    "  • MEDDPICC pillars CONFIRMED from real buyer events (+1 each) ....... 0–4\n"
    "  • ≥10 buyer-side users onboarded ................................... 1\n"
    "  • economic buyer had DIRECT Zycus face time ........................ +3  ← a BOOSTER, never a gate\n"
    "Tiers: QDI ≥12 = Tier 3 (deep) · 8–11 = Tier 2 · 5–7 = Tier 1 · <5 = none.\n"
    "Effect — Tier 3 → Win FLOOR 48 and the staleness multiplier in 4.4 floors at ×0.6; Tier 2 → "
    "floor 42, ×0.5; Tier 1 → floor 36, ×0.4.\n"
    "GUARDS (depth is credit, never a free pass): (a) NO-EB CAP — without direct EB face time the "
    "depth floor may NOT exceed 48; deep work with no economic buyer is a re-winnable position, not "
    "a strong one. (b) A section-5 ceiling ALWAYS binds (Formal Eval / Shortlisted stays ≤60). "
    "(c) 4.4b still SUPERSEDES this floor. (d) Depth must be HARD — real buyer events in the record, "
    "never rep-claimed depth, a single demo, or a buyer-voiced loss. (e) Lives in Win ONLY; Momentum "
    "still reflects that the deal is quiet now.\n"
    "Rationale must name the tier and its evidence, e.g. 'Depth Tier 3 (QDI 18): 17-user POC, buyer-"
    "run scorecard 22 May, 14 buyer attendees across 3 Bosch entities — floor 48 applied, capped at "
    "48 (no EB face time).' Absent hard depth, the standard 4.4 decay applies "
    "and a genuinely cold or shallow dark deal still scores low.")

W_44B_OLD = ("when the deal is LATE-STAGE (recorded stage Vendor Selected or later) AND Deal Momentum "
             "< 30 (the same 0–100 Momentum score produced for this deal on this evidence), HALVE the "
             "Win Position")
W_44B_NEW = ("when the deal is LATE-STAGE (recorded stage Vendor Selected or later) AND Deal Momentum "
             "< 30 (the same 0–100 Momentum score produced for this deal on this evidence) AND the deal "
             "is NOT in Momentum's process-mode (i.e. there is no live, dated, buyer-set FUTURE "
             "milestone), HALVE the Win Position")

W_45_OLD = ("4.5 ENGAGEMENT PULSE (own read, ±15). Read live-vs-dark directly (do NOT import Momentum's "
            "number):")
W_45_NEW = (
    "4.5 ENGAGEMENT PULSE (own read, ±15). Read live-vs-dark off BUYER ACTIONS as defined in 4.4 — "
    "not meetings alone. A buyer-issued RFP round, deadline or clarification inside 30d IS a live "
    "signal. While a buyer-set milestone is open and unexpired, do NOT apply a dark penalty for "
    "meeting gaps shorter than the round's own cadence. (v10.8) Read live-vs-dark directly (do NOT "
    "import Momentum's number):")

W_56 = (
    "\n## 5.6 RFP-ADVANCEMENT FLOOR (added v10.8)\n"
    "Surviving a competitive cut is a buyer action, and the engine must credit it. When the buyer has "
    "CONFIRMED our advancement to a further round of a live structured evaluation (round 2 / BAFO / "
    "finalist / shortlist retained after a down-select) within the last ~30 days, AND no rival is "
    "confirmed ahead of us, hold a Win FLOOR of 45. A 4-5 supplier long list narrowed to a shortlist "
    "that still includes us is evidence we are winning ground, not losing it. This floor NEVER "
    "breaches the 5.1 stage ceiling (Formal Eval / Shortlisted stays ≤60) and does NOT fire on: our "
    "own intent to bid, an open RFI, a rep-claimed preference, or a round we have not been confirmed "
    "into. If a rival IS confirmed ahead (buyer-voiced), this floor is void.\n")

W_8D = (
    "\n\n## 8d. Depth + RFP-advancement acceptance (v10.8)\n"
    "Shortlisted, 17-user POC executed, buyer ran its own scorecard, 14 buyer attendees, 3 MEDDPICC "
    "pillars confirmed, NO economic-buyer face time, buyer issued round-2 docs 10d ago, last meeting "
    "44d ago → Depth Tier 3, floor 48 capped at 48 by the no-EB cap, buyer-action clock = 10d (×1.0 "
    "decay), 5.6 advancement floor also 45 → Win ≈ 50–55, ceiling 60. NOT ~36.\n"
    "Same deal, but a rival is buyer-confirmed ahead → 5.6 void.\n"
    "Vendor Selected, deep qualification, momentum 8, 330d dark, NO live buyer milestone → 4.4b fires "
    "(process-mode off), Win halved to ~20. Depth does NOT rescue it.")

# ---------------------------------------------------------------- MOM edits
M_49_OLD = ("4.9 STALLING DRAG (0…−25): days with no genuine engagement/CRM advance: ≤30d 0 · 31–60d "
            "ramps 0→−12 · 61–90d ramps −12→−25 · >90d −25. Rep chasing doesn't reset the clock. "
            "Suspended in process-mode.")
M_49_NEW = (
    "4.9 STALLING DRAG (0…−25): days since the last BUYER ACTION on ANY channel — a meeting, a buyer "
    "reply, a buyer-ISSUED document or deadline (new RFP round, BAFO invitation, finalist notice, "
    "scoring request), a buyer clarification round, or a buyer portal update. A buyer-issued document "
    "or deadline RESETS the clock; rep chasing NEVER does. (v10.8: this counted only meetings/"
    "completed activity, so a buyer who had just issued round-2 documents read as 44 days dark.) "
    "≤30d 0 · 31–60d ramps 0→−12 · 61–90d ramps −12→−25 · >90d −25. "
    "DEPTH SOFTENER: when the deal is Qualification-Depth Tier ≥2 (see Zycus Win Position §4.4a) AND "
    "a buyer-set future milestone is open and unexpired, HALVE the ramp (31–60d: 0→−6; 61–90d: "
    "−6→−12). With no open buyer milestone the full ramp returns — depth must not shelter a zombie. "
    "Suspended entirely in process-mode.")

M_410 = (
    "\n4.10 ENGAGEMENT TRAJECTORY (±8, added v10.8) — the frequency factor. Compare BUYER-action "
    "density over the last 45 days against the prior 45 days. Rising density → up to +8; flat → 0; "
    "falling → down to −8. In process-mode, compare deliverable-to-deliverable spacing against the "
    "round's OWN cadence instead of a calendar window. A burst of activity followed by quiet is "
    "NEUTRAL (0) when a buyer-set milestone is open and unexpired — that is normal procurement "
    "rhythm, not decay. The same burst-then-quiet with NO open milestone is negative. Answer the CRO's "
    "real question: after all that engagement, did this deal go from strength to strength, or did it "
    "slow down? Say which, and off what evidence.")

M_5_OLD = ("Enter when ALL: structured stage (Formal Eval/Shortlisted/Vendor-Selected-in-procurement) + "
           "a live, dated, FUTURE milestone + on-track (last deliverable on time, buyer not paused).")
M_5_NEW = (
    "Enter when ALL: structured stage (Formal Eval/Shortlisted/Vendor-Selected-in-procurement) + a "
    "live, dated, FUTURE milestone + the BUYER's process is on-track. ON-TRACK IS ABOUT THE BUYER, "
    "NOT US (clarified v10.8): a future buyer-set milestone exists, no buyer deadline has passed in "
    "silence, and the buyer has not paused or postponed. A deliverable WE owe the buyer — a late "
    "proposal, overdue API docs, an unsent reference — NEVER exits process-mode and NEVER drags "
    "Momentum. It is an EXECUTION RISK: report it in Deal Risk and as a ► intervention on us. "
    "(Before v10.8 our own 6-week-late API docs disqualified a live RFP from process-mode and cost "
    "the deal ~20 momentum points — the buyer was engaging the whole time.)")

M_5_LADDER_OLD = "- Deliverables ARE engagement (×1.0): RFP/tender received 6 (buyer's skin in the game)"
M_5_LADDER_NEW = (
    "- Deliverables ARE engagement (×1.0): buyer ISSUES a new RFP round / BAFO invitation / finalist "
    "notice / scoring request 8 (the strongest non-meeting buyer action there is — credit it at the "
    "ISSUE date's recency, not the last meeting's) · RFP/tender received 6 (buyer's skin in the game)")

M_6_OLD = ("momentum = clamp( 35 + engagement(cap+50) + next_step(cap+10) + history(cap+10) + "
           "future_meeting(cap+5) + forecast_move(±6) + close_date(−10..+5) − one_way(0..−6) − "
           "passivity(0..−8) − stalling_drag(0..−25, 0 in process-mode), 0, 100 )")
M_6_NEW = ("momentum = clamp( 35 + engagement(cap+50) + next_step(cap+10) + history(cap+10) + "
           "future_meeting(cap+5) + forecast_move(±6) + close_date(−10..+5) + trajectory(±8) − "
           "one_way(0..−6) − passivity(0..−8) − stalling_drag(0..−25, halved at Depth Tier ≥2 with an "
           "open buyer milestone, 0 in process-mode), 0, 100 )")

M_8_OLD = "live RFP, 40d quiet, deliverables landing → ≥45."
M_8_NEW = (
    "live RFP, 40d quiet, deliverables landing → ≥45 · "
    "live RFP, buyer ISSUED round-2 docs 10d ago, 44d since the last meeting, OUR API docs 6 weeks "
    "overdue → process-mode ON (our lateness is a Deal Risk, not a momentum penalty), buyer-action "
    "clock = 10d, round-2 issuance credited 8 → momentum ≥50; the overdue doc appears in Deal Risk "
    "with a ► on us · "
    "deep POC + buyer scorecard (Depth Tier 3), buyer milestone open, 50d since last meeting → "
    "stalling ramp halved · same deal with NO open buyer milestone → full ramp (no zombie shelter).")


def latest(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "is.true",
                                "select": "id,version,content"},
                        headers=H, verify=False, timeout=60).json()
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    return rows[0]


def sub(txt, old, new, label):
    if old not in txt:
        raise SystemExit(f"ANCHOR MISSING [{label}] — aborting, no partial edits.\n  {old[:110]}…")
    if txt.count(old) != 1:
        raise SystemExit(f"ANCHOR AMBIGUOUS [{label}] ({txt.count(old)} matches) — aborting.")
    print(f"   ✓ {label}")
    return txt.replace(old, new)


def replace_block(txt, head, tail, new, label):
    i = txt.find(head)
    j = txt.find(tail, i)
    if i < 0 or j < 0:
        raise SystemExit(f"BLOCK MISSING [{label}] — aborting.")
    print(f"   ✓ {label} (replaced {j + len(tail) - i:,} chars)")
    return txt[:i] + new + txt[j + len(tail):]


win = latest("win")
mom = latest("mom")
print(f"base: win v{win['version']} ({len(win['content']):,} chars) · "
      f"mom v{mom['version']} ({len(mom['content']):,} chars)\n")

print("WIN 10.8 edits:")
w = win["content"]
w = sub(w, W_431_OLD, W_431_NEW, "4.3 deal-strengthening compound (±12)")
w = sub(w, W_44_OLD, W_44_NEW, "4.4 decay clock -> BUYER ACTION")
w = replace_block(w, W_44A_HEAD, W_44A_TAIL, W_44A_NEW, "4.4a QDI depth floor (measured, not gated)")
w = sub(w, W_44B_OLD, W_44B_NEW, "4.4b gate requires process-mode OFF")
w = sub(w, W_45_OLD, W_45_NEW, "4.5 pulse reads buyer actions")
w = sub(w, "\n## 6. Bands", W_56 + "\n## 6. Bands", "5.6 RFP-advancement floor")
w = w.rstrip() + W_8D
w = w.replace("# ZYCUS WIN POSITION — SYSTEM INSTRUCTION · v10.0",
              "# ZYCUS WIN POSITION — SYSTEM INSTRUCTION · v10.8", 1)

print("\nMOM 10.8 edits:")
m = mom["content"]
m = sub(m, M_49_OLD, M_49_NEW, "4.9 stalling clock -> BUYER ACTION + depth softener")
m = sub(m, M_49_NEW, M_49_NEW + M_410, "4.10 engagement trajectory (frequency factor)")
m = sub(m, M_5_OLD, M_5_NEW, "5. process-mode on-track = BUYER's process")
m = sub(m, M_5_LADDER_OLD, M_5_LADDER_NEW, "5. round-issuance = 8 (strongest non-meeting action)")
m = sub(m, M_6_OLD, M_6_NEW, "6. formula + trajectory + softened drag")
m = sub(m, M_8_OLD, M_8_NEW, "8. acceptance tests (Bosch case)")

print(f"\nresult: win 10.8 = {len(w):,} chars (+{len(w) - len(win['content']):,}) · "
      f"mom 10.8 = {len(m):,} chars (+{len(m) - len(mom['content']):,})")

if not APPLY:
    print("\nDRY RUN — nothing written. Re-run with --apply to lock.")
    open("cc_work/_win108.md", "w", encoding="utf-8").write(w)
    open("cc_work/_mom108.md", "w", encoding="utf-8").write(m)
    print("previews: cc_work/_win108.md, cc_work/_mom108.md")
    raise SystemExit(0)

NOTE = ("v10.8 (user-approved): buyer-action clock replaces the meeting-only clock; process-mode "
        "on-track is the BUYER's process (our late deliverable is a Deal Risk, not a momentum "
        "penalty); qualification depth is MEASURED (QDI tiers) not gated on EB face time; "
        "deal-strengthening compound ±12; RFP-advancement floor 45; engagement trajectory ±8. "
        "Guards kept: stage ceiling, no-EB cap 48, 4.4b momentum gate (ACEN still halves).")
for eng, content in (("win", w), ("mom", m)):
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=representation"},
                      json={"engine": eng, "version": "10.8", "content": content, "kind": "minor",
                            "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                      verify=False, timeout=90)
    print(f"  lock {eng} v10.8 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:200]}")
print("\nLOCKED. active_locked() will serve v10.8 on the next sweep — no deploy needed.")
