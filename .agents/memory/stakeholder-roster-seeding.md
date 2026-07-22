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

**Scope boundary (deliberate — do NOT "fix").** Roster = **verified SFDC contacts only**.
A 2026-07-22 read-only audit of the three opps confirmed the only residual misses are
people with NO SFDC contact record — Russell's 8 Avoma call attendees, McAfee's Rohith
Shankar (+ 2 CC'd), Bass Pro's Chris Rodgers — all named only in Avoma attendee lists /
email signatures, none present as Salesforce contacts (verified by SOQL). The user
**explicitly chose "verified-contacts-only"** over adding an "also engaged (not in CRM)"
tier. So the narrative seed intentionally does NOT mine Task/Event activity bodies, and
there is no unlinked-participant tier. Don't add one — revisit only if that product
decision is reversed.

**Verified 2026-07-22** on live `mase-worker:285`: Russell 4→6 (recovered Bhaskar Pandey,
Marci Jasinski), McAfee 5→6 (Mazen El-Haidari), Bass Pro 5→7 (Kory Cooper, David Gouvion);
no "Global Sourcing" phantom. The prompt-only lever (sweep studio v10.7) did NOT fix this.

Related: [[buyer-identity-account-fallback]] (how the full contact pool + narrative are
fetched and injected), [[prompts-source-of-truth]], [[salesforce-id-15-vs-18]].
