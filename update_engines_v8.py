"""Omnivision Studio — SUM 10.2 -> 10.3 (user-approved 2026-07-11).

The 24-Hour Summary read "textbook-ish" because the prompt itself demanded BREVITY —
"a DELTA read ... ONE headline line, plus up to 2 supporting lines ... keep each line short."
v10.3 reframes it into a DETAILED intelligence briefing: still a delta (only what changed in
the window), but richly narrated — who did what, why, what was asked/delivered, what's pending,
and the implication — with named (SF-grounded) people, buyer quotes, dates, dollars, competitors.
Detail and specificity are the goal; brevity is NOT. Grounding is unchanged: executed-change
only, never invent — detailed must never mean fabricated.

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
BY = "omnivision-sum-detail-2026-07-11"

# 1) §1 — reframe brevity -> detailed briefing
A_OLD = ("A 24-Hour Summary for a deal: a DELTA read that reports ONLY what changed since the last "
         "window, framed for the buyer-facing stakeholders who will read it. Standalone — it does "
         "NOT depend on any other engine.")
A_NEW = ("A 24-Hour Summary for a deal: a DETAILED intelligence briefing on what changed since the "
         "last window, framed for the deal owner and buyer-facing stakeholders who will read it. "
         "Standalone — it does NOT depend on any other engine. Write it RICH, not terse: a reader "
         "must grasp the FULL STORY of the window — who did what, why, what was asked or delivered, "
         "what is now pending, and what it MEANS for the deal — WITHOUT opening the record. Detail, "
         "specificity and insight are the goal; brevity is NOT a goal. (It stays a DELTA — narrate "
         "what CHANGED in the window in depth, do not re-summarise the whole deal.)")

# 2) §3 — expand the shape from "headline + 2 short lines" to a rich narrative + detailed items
B_OLD = ("- ONE headline line, plus up to 2 supporting lines when more than one qualifying change "
         "exists. If only one thing changed → produce only the headline. If nothing changed → say "
         "so plainly (§5).\n- Keep each line short and BUYER-FRAMED. Frame engagement events from "
         "the buyer's actions (\"Buyer returned the security questionnaire\"). For internal CRM "
         "field changes (forecast, amount, close date, stage, score) → translate into "
         "buyer-relevant language where you can, otherwise report the fact plainly.")
B_NEW = ("- A RICH NARRATIVE headline that tells the full story of the window — typically 3-5 "
         "sentences: what happened, who drove it (name the real, Salesforce-grounded people), why "
         "/ what prompted it, where the deal now stands, what is pending, and the IMPLICATION for "
         "the deal. When two important things happened, weave both in (gain first, risk as the "
         "tail). If genuinely nothing changed → say so plainly (§5) — do NOT pad a quiet day.\n"
         "- For EACH activity/change in the window, a DETAILED intelligent read (2-4 sentences): "
         "for an EMAIL — who sent it to whom, why / what it replies to, what it asked for or "
         "delivered, and whether a reply is now pending; for a MEETING/CALL — who met, what was "
         "discussed, and what was DECIDED or concluded (summarise the substance, never paste the "
         "transcript); for a MOVEMENT — what the field change SIGNALS (amount cut = scope/budget "
         "pullback; close pushed = slip, name the new date; stage moved despite a stalled buyer = "
         "a CRM-vs-reality mismatch, call it out). Be SPECIFIC: quote the buyer where it sharpens "
         "the point, cite dates, dollar figures, and competitor names.\n"
         "- BUYER-FRAMED. Frame engagement events from the buyer's actions (\"Buyer returned the "
         "security questionnaire\"). For internal CRM field changes → translate into "
         "buyer-relevant language where you can, otherwise report the fact plainly. DETAIL OVER "
         "BREVITY — but every sentence must be grounded in an actual executed change/event; "
         "detailed must NEVER mean invented, padded, or speculative.")

# 3) §7 — output shape matches the richer spec
C_OLD = ("A single headline line (buyer-framed), plus up to 2 supporting lines when qualifying "
         "changes exist; gain-then-risk order within a line where both are present. Or a plain "
         "\"No change in the last 24h\" — with an optional \"In the last 48h: …\" side note when "
         "the safety-net applies. Short, stakeholder-readable, executed-change only.")
C_NEW = ("A rich narrative headline (the full story of the window, ~3-5 sentences, gain-then-risk "
         "where both exist) PLUS a detailed intelligent read for each activity/change in the "
         "window (2-4 sentences each, named people + specifics). Or a plain \"No change in the "
         "last 24h\" — with an optional \"In the last 48h: …\" side note when the safety-net "
         "applies. Stakeholder-readable, executed-change only, grounded — detailed but never "
         "fabricated.")


def latest(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "is.true",
                                "select": "id,version,content"},
                        headers=H, verify=False, timeout=60).json()
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    return rows[0]


def sub(txt, old, new, label):
    if txt.count(old) != 1:
        raise SystemExit(f"ANCHOR [{label}] appears {txt.count(old)}x — aborting.")
    print(f"   ok {label}")
    return txt.replace(old, new)


s = latest("sum")
print(f"base: sum v{s['version']} ({len(s['content']):,} chars)\n")
t = s["content"]
t = sub(t, A_OLD, A_NEW, "§1 detailed-briefing reframe")
t = sub(t, B_OLD, B_NEW, "§3 rich narrative + detailed items")
t = sub(t, C_OLD, C_NEW, "§7 output shape")
t = t.replace("# ZYCUS 24-HOUR SUMMARY — SYSTEM INSTRUCTION · v10.0",
              "# ZYCUS 24-HOUR SUMMARY — SYSTEM INSTRUCTION · v10.3", 1)
print(f"\nresult: sum v10.3 = {len(t):,} chars (+{len(t)-len(s['content']):,})")

if not APPLY:
    open("cc_work/_sum103.md", "w", encoding="utf-8").write(t)
    print("\nDRY RUN — wrote cc_work/_sum103.md. Re-run with --apply to lock.")
    raise SystemExit(0)

NOTE = ("v10.3 (user-approved): reframed the 24h summary from a terse delta (headline + 2 short "
        "lines) into a DETAILED intelligence briefing — rich narrative headline (3-5 sentences) + "
        "a detailed per-activity read (2-4 sentences, named people, quotes, dates, dollars, "
        "competitors, the implication). Grounding unchanged: executed-change only, never invent.")
r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "sum", "version": "10.3", "content": t, "kind": "minor",
                        "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                  verify=False, timeout=90)
print(f"lock sum v10.3 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:200]}")
print("LOCKED — active_locked() serves sum v10.3 on the next AI-summary run." if r.status_code < 300 else "FAILED")
