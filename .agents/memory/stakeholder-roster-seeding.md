# Stakeholder roster = seeded membership + LLM enrichment

**Rule.** The Stakeholders tab must list everyone *demonstrably engaged* on a deal, not
just whoever the agent chose to mention. Membership is decided **deterministically from
fact**; the LLM only **enriches** (title, role, sentiment, "why they matter"). Never let
the agent decide *membership* — it under-includes quiet participants (adherence problem,
not a wiring problem: the full contact list, narrative and attendee roster were already in
the prompt and the agent still skipped people).

**Where.** `deal_engine_sweep._roster_from_sfdc(new_ai, buyer, existing_record,
attendee_roster=...)`, called from `analyze_one`. The `_seed(contact, why)` pass adds, if
not already mapped, and enriches from the matching AI item (fold-key) else tags
`_source=why`:
1. **every** Opportunity Contact Role,
2. every **call attendee** that resolves to a real SFDC contact — roster built by
   `_buyer_attendee_roster` from the Avoma datalake `attendee_objs` (named objects; the
   sparse `attendee_emails` field is not enough — Bass Pro showed 3/11 there),
3. every **narrative-named** SFDC contact — whole first+last-name match against
   Description / Next Step / Next Step History (`_narr`).

**Guards.**
- `_DEPT_WORDS` frozenset: a "contact" whose tokens are ALL role/department words
  ("Global Sourcing", "Procurement Team") is skipped in the narrative seed — a department
  is not a person.
- Anti-fabrication is intact: AI-invented names with no SFDC anchor are still dropped.
- `_ROSTER_CAP` = 12.

**Why not deterministic force-feed.** We explicitly rejected dumping the whole account
list into the roster. Seed-from-fact + LLM-enrich keeps the judgement work (role/sentiment)
with the agent while making it impossible to drop a proven-engaged person.

**Scope (updated 2026-07-23 — "verified-only" PARTIALLY REVERSED).** History: a 2026-07-22
audit found the only residual misses were people with NO SFDC contact record (Bass Pro's
Chris Rodgers, Radicare's Khuza Ashikin, etc.) and the user first chose "verified-contacts-
only". On 2026-07-23 the user **reversed that**: the roster NOW adds a person who **sat in a
recorded call** on the **buyer email domain** but has no SFDC contact — via
`_seed_attendee_no_crm`, flagged `_not_in_crm=True`, seeded LAST so it never bumps a
verified contact under the cap. Guards: requires a buyer-domain email; skips rooms/bots/
mailers (name regex) and pure departments (`_DEPT_WORDS`).

**Still excluded (the bar is CALL ATTENDANCE, not mention):** email-only / narrative-only
names that never attended a call (e.g. Bass Pro "Sara Walker perhaps?" — a rep's guess in
one email). The narrative seed still only anchors to real SFDC contacts; it does NOT mine
Task/Event bodies. So: on a call + buyer domain → added even if not in CRM; merely mentioned
→ still needs a real SFDC contact.

**Verified 2026-07-22** on live `mase-worker:285`: Russell 4→6 (recovered Bhaskar Pandey,
Marci Jasinski), McAfee 5→6 (Mazen El-Haidari), Bass Pro 5→7 (Kory Cooper, David Gouvion);
no "Global Sourcing" phantom. The prompt-only lever (sweep studio v10.7) did NOT fix this.

Related: [[buyer-identity-account-fallback]] (how the full contact pool + narrative are
fetched and injected), [[prompts-source-of-truth]], [[salesforce-id-15-vs-18]].
