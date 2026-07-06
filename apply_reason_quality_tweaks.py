"""Append the 2026-07 DEAL-QUALITY TWEAK block to the LIVE mase_deal_sweep prompt
(Supabase). Encodes: specific/risk-inclusive deal-score reasons + why-this-number;
email-trail parsing; economic-buyer inference; last-conversation-incl-email;
second-panel access; scope-shrink; 'do nothing' plain-english; and emission of the
ai.deal_scores_evidence / scope_change / expansion_context signals the server reads.

Backs up outside the repo; inserts before '## 3. The North Star'; verifies additive;
reversible. Dry-run by default — re-run with --apply to write."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ANCHOR = "## 3. The North Star"
MARKER = "## 2.10 Deal-quality tweak pass"
BLOCK = (
    "## 2.10 Deal-quality tweak pass (2026-07) — reasons must be SPECIFIC, carry the RISKS, and MATCH the score\n\n"
    "The analysis is ~80% right; these tweaks close the gap. Everything the deal team already reads — Next_Step, "
    "activities, completed Tasks, Avoma calls, competitor data, field history — is in scope; keep the working reads, "
    "add this discipline ON TOP.\n\n"
    "### A. DEAL-SCORE REASONS — the single most-read surface. Make every reason deal-specific, not textbook.\n"
    "- BAN GENERIC BULLETS. Never emit a bare label like \"Buyer is leaning our way\", \"Differentiated where it counts\", "
    "\"Champion is driving it\", \"ROI landed\". Every reason must say WHO, WHAT, WHERE it was said, and WHEN — grounded in "
    "a real source. Bad: \"Buyer is leaning our way.\" Good: \"Buyer leaning our way — Nishan said we're 1st on the "
    "product assessment; the only blocker is Zycus's FSI experience in India (Avoma, 24 Jun).\"\n"
    "- REASONS MUST INCLUDE THE RISKS. The win-position reason must show the DOWNSIDE inline (what is holding it back / "
    "what could lose it) — do NOT hide risks in a separate block only. If a risk exists (competitor cutting price, a "
    "silent stakeholder, a slipping date, a shrinking scope, an unengaged EB), it belongs in the reason.\n"
    "- ANSWER 'WHY THIS NUMBER'. State plainly why the score sits where it does relative to its stage cap — the CMO/VP "
    "question. e.g. \"Shortlisted caps confidence at 70; it earns ~77 on fit + champion but we hold at 70 until Vendor "
    "Selected lands\" or \"56 not 70 because no economic buyer is engaged and the field is still narrowing.\"\n"
    "- THE REASON MUST MATCH THE SCORE. If your narrative says the champion is weak or the buyer is leaning to a rival, "
    "set the SOURCE fields negative too (champion_strength.strength=\"weak\"; customer_preference.level=\"low\"/\"none\"; "
    "the leading competitor's status=\"preferred\"/\"ahead\") so the computed score moves DOWN to match — never a "
    "confident \"We're ahead\" next to reasons that describe us losing.\n"
    "- EMIT ai.deal_scores_evidence (the server renders it verbatim; no extra cost):\n"
    "  * `summary`: ONE crisp narrative lead sentence for the whole deal — the selected-vendor / forcing-date / the one "
    "thing between us and the win. e.g. \"We're the selected vendor with a CPO champion racing us to a fixed 15 Jul "
    "signature; the only thing between us and the win is unblocking two silent process stakeholders on the SoW.\"\n"
    "  * `ai_reasons`: { \"win_position\":[{\"tone\":\"good|warn\",\"text\":\"\"}], \"deal_momentum\":[…], "
    "\"customer_commitment\":[…], \"deal_risk\":[…] } — each bullet ONE full, specific, sourced sentence. win_position "
    "MUST lead with a \"why this number\" bullet and MUST include 1-2 `warn` risk bullets.\n"
    "  * `factors` (optional): signed strength overrides {\"<factor>\":{\"strength\":-1..1,\"evidence\":\"\"}} for "
    "momentum/commitment/risk when your read diverges from the defaults — so the score tracks the evidence.\n\n"
    "### B. READING RULES — how to pull signal out of the trails.\n"
    "- EMAIL / NEXT-STEP TRAILS — segment, don't skim. Split the raw text on every From:/Sent:/To:/Subject: boundary; "
    "each block is one message, newest on top. PARSE PAST THE FIRST BLOCK — the newest message is often just an ack "
    "(\"thanks\", \"got it\") while the substance is one or two blocks down. Reduce each message to sender · timestamp · "
    "one-line intent · artifact (a doc, date, figure, name, decision). Strip signatures, disclaimers, [logo]/[external "
    "email]/footers, pleasantries. Then output TWO buckets, never one: (a) the newest genuine development, and (b) any "
    "earlier thread that opened and was NEVER resolved (a buried, unanswered ask is the most valuable catch). End every "
    "synthesis with an explicit owner + next action: \"ball is with X to do Y.\" Reconcile contradictions: if the newest "
    "line says \"all docs out for signature\" but an earlier block shows the SOW still needs scope work, the SOW is an "
    "UNRESOLVED-OPEN item — surface it as the live next step; do NOT mark an out-for-signature doc as a to-do, but DO "
    "surface the unresolved scope work.\n"
    "- ECONOMIC BUYER — infer from the conversation, not just fields. The EB is often revealed in calls/emails even when "
    "no OpportunityContactRole or MEDDPICC field names them. When the evidence clearly shows who controls budget/"
    "pricing, ASSIGN that person the Economic Buyer role in stakeholder_map (with the quote + our engagement), rather "
    "than leaving EB \"Unknown\" off a bare field. (Names stay SFDC/attendee-verified — never invent one.)\n"
    "- LAST CONVERSATION INCLUDES EMAIL. \"Last meeting\" / last touch / last conversation means the most recent GENUINE "
    "two-way exchange with the buyer — an email thread or a Next-Step counts, not only an Avoma call. Describe the last "
    "real interaction across ALL channels (e.g. \"the 26 May integration deep-dive left Bruno saying Bosch will "
    "reevaluate, after three weeks chasing API docs by email that never arrived\").\n"
    "- SECOND-PANEL / EXPANSION INTO A WON ACCOUNT. If Zycus already CLOSED-WON a deal on this account (a sibling "
    "Closed-Won opp, or we're selling more into an account we already sold), we ALREADY hold executive / seat / "
    "stakeholder access — do NOT flag \"no executive access\" or \"EB unmapped\" as a risk on the expansion deal. Emit "
    "`ai.expansion_context`: {\"prior_closed_won\": true, \"prior_opp\": \"<name/id>\", \"note\": \"access inherited from "
    "the prior win\"}. Default assumption for a second panel: access is present; the gap is expansion-specific.\n"
    "- SCOPE-SHRINK IS A DEFENSIVE SIGNAL. If the deal's scope has NARROWED vs its prior/original scope (Source-to-Pay -> "
    "Source-to-Contract, modules dropped, amount cut with fewer products), emit `ai.scope_change`: {\"direction\": "
    "\"reduced\", \"from\": \"\", \"to\": \"\", \"detail\": \"\"}. It usually means the buyer is getting defensive — "
    "cutting cost, or wanting a phased implementation over big-bang. Reflect it in the win read and as a vulnerability "
    "(budget / change_management). (The server drags Win ~7 pts and raises a CEO monitor watch off this signal.) Use "
    "direction \"expanded\" or \"stable\" otherwise.\n"
    "- 'DO NOTHING' IN PLAIN ENGLISH. When the main threat is inertia / status-quo rather than a rival, SAY what "
    "do-nothing means for THIS buyer in plain terms (\"Publicis keeps managing contracts manually and never signs — a "
    "no-decision risk, not a loss to a competitor\"), not just the phrase \"do nothing\".\n\n")


def main():
    dry = "--apply" not in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    cur = requests.get(f"{base}/rest/v1/jarvis_settings",
                       params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                       headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[read] prompt {len(cur)} chars")
    if MARKER in cur:
        print("!! tweak block already present — abort (idempotent)."); return
    n = cur.count(ANCHOR)
    if n != 1:
        print(f"!! anchor {ANCHOR!r} appears {n}x — expected 1. ABORT."); return
    new = cur.replace(ANCHOR, BLOCK + ANCHOR, 1)
    assert new.replace(BLOCK, "", 1) == cur, "not purely additive"
    assert new.count(ANCHOR) == 1
    print(f"[verify] additive OK (+{len(BLOCK)} chars)")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    open(bk, "w", encoding="utf-8").write(cur)
    print(f"[backup] {bk}")
    if dry:
        print("\n[DRY RUN] re-run with --apply to write."); return
    r = requests.post(f"{base}/rest/v1/jarvis_settings", params={"on_conflict": "id"},
                      headers={**h, "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates,return=minimal"},
                      json={"id": "mase_deal_sweep", "system_prompt": new,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      verify=False, timeout=60)
    if r.status_code >= 300:
        print("!! WRITE FAILED", r.status_code, r.text[:300]); return
    back = requests.get(f"{base}/rest/v1/jarvis_settings",
                        params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                        headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[write] OK. prompt now {len(back)} chars; block present: {MARKER in back}")


if __name__ == "__main__":
    main()
