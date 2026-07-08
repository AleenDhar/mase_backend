# Zycus Deal-Progression Playbook — how a deal moves from Qualified to PO (MASE knowledge base)

> **What this is.** A single, exhaustive reference for how a Zycus **new-business enterprise
> deal** actually progresses — the sales motion, the milestones and artifacts you encounter at
> each stage, what "ideal" looks like, and the post-selection contracting paper trail. It is
> written for **LLMs and analysts** reading a deal: so a sweep, a chat agent, or a human can look
> at a Salesforce stage + a pile of calls/emails and know *what should be happening now, what
> comes next, and whether the deal is where it should be.*
>
> **Sources (all in this repo / the live system):** the live `mase_deal_sweep` Supabase prompt
> (the "enterprise motion" + stage→milestone map + verdict rails), `docs/zycus-contracting-reference.md`
> (the post-shortlist paper trail), the deterministic scoring engine (`deal_engine_scoring.py`,
> `deal_engine_footprints.py`, `deal_engine_verdict.py` — stage numbers, cadence, the
> engagement-depth ladder, MEDDPICC), and the `knowledge_index/` Showpad cards (products,
> implementation, support, competitive). Where a fact needs verification before customer-facing
> use, it is flagged **(verify)**.
>
> **Scope: NEW-BUSINESS deals.** Renewals, change-requests, cross-sell/upsell and Certinal
> annual-invoicing follow a lighter path and are called out where they differ.

---

## 0. The shape of a Zycus enterprise deal (orientation)

- **Big, committee-driven, long.** A typical new-business enterprise deal is a **multi-person
  buying committee** on a **12–15 month cycle**. Real milestones are often **weeks or months
  apart**, so long gaps between formal events are *normal* — a quiet stretch is not automatically
  a stall.
- **Champion-building runs continuously** underneath every stage — it is not a step, it is the
  spine.
- **Grade the BUYER, not the rep.** A rep sending emails into silence is *not* momentum. Deal
  health is measured by the buyer's engagement on the agreed next step, not by rep activity.
- **The close date is the North Star.** Everything is benchmarked against it: is the deal moving
  at a pace that makes the date credible?

---

## 1. The full enterprise motion (the canonical spine)

The complete new-business motion, in order (from the live sweep prompt §4):

```
discovery call
  → demos
    → RFI round
      → RFP round
        → vendor shortlisting
          → ShoeFit / BRD fit  (weed out misfits vs the buyer's Business Requirements Document)
            → deeper / use-case demos
              → half/full-day customer workshops  (OPTIONAL — not every buyer)
                → commercials & pricing
                  → multi-round negotiation  (term, services, config/custom dev, AI/Merlin, credits, partner)
                    → ROI workshop
                      → proposal to the Economic Buyer / CFO / C-level sponsor
                        → reference-customer calls
                          → Horizon (Zycus customer/prospect event) pull-in
                            → InfoSec review + ERP / systems-integration deep-dive
                              → contracting (SOW, MSA, redlining)
                                → close
[ champion-building runs continuously beneath all of it ]
```

Not every deal hits every step, and the order flexes (a POC-led motion front-loads a POC; a
tender compresses discovery). Use this as the reference spine to locate "where are we, and what
is the realistic next milestone."

---

## 2. Stage-by-stage playbook

Salesforce stage order (low → high): **Initial Interest → Qualified → Formal Evaluation →
Shortlisted → Vendor Selected → Negotiation → Contract In Progress → Contract Signed →
PO Received** (terminal: Closed Won / Closed Lost / Qualified Out / Omitted).

For each stage below: the **engine calibration** (how the scorer benchmarks it), **what you'll
encounter**, **what "good/ideal" looks like**, and **the next milestone to drive toward**.

### Engine calibration cheat-sheet (all stages)

The deterministic scorer encodes the "shape" of each stage. These numbers are how the machine
reasons; **never quote them in a human-facing reason** — they are the calibration, not the story.

| Stage | Win anchor (prior) | Win **ceiling** | Expected momentum | Buyer cadence (days) | Tier |
|---|---|---|---|---|---|
| Initial Interest | 8 | **30** | 48 | 30 | early *(sweep skips)* |
| Qualified | 18 | **30** | 50 | 30 | early |
| Formal Evaluation | 35 | **70** | 52 | 21 | early→mid |
| Shortlisted | 55 | **70** | 56 | 18 | mid |
| Vendor Selected | 72 | **100** | 60 | 14 | mid |
| Negotiation | 85 | **100** | 62 | 21 | late |
| Contract In Progress | 85 | **100** | 62 | 21 | late |
| Contract Signed | 95 | **100** | 55 | 30 | late |
| PO Received | 98 | **100** | 55 | 45 | late |

**The ceiling doctrine (critical):** you cannot be highly confident of winning until the buyer is
structurally committed.
- **Pre-RFP** (Initial Interest, Qualified) → Win capped at **30**.
- **In the RFP round** (Formal Evaluation, Shortlisted) → Win capped at **70**. *Crossing 70 means
  you've been selected — a still-evaluating deal cannot read like a near-certain win.*
- **Post-shortlist** (Vendor Selected → PO) → up to **100**.
- **Access to Power gates the top even more:** with no economic buyer engaged, Win is capped at
  **52** regardless of stage/momentum. A selection is *made by* an economic buyer — with none on
  record, no one has selected you. (See §7.)

---

### Stage 1 — Qualified *(and Initial Interest)*

- **Meaning:** the opportunity is real and worth pursuing; discovery is underway. Initial Interest
  is so early the sweep skips it.
- **What you'll encounter:** discovery calls (the call where the buyer often names their *whole
  competitive shortlist* — capture it), first **standard demos**, opening pain/scope conversations,
  early RFI. A short logistics/relationship call with a senior buyer (e.g. a CPO) still counts as
  a real engagement signal.
- **Ideal / "good":** quantified pain (not just "it's manual"), the **Economic Buyer and Decision
  Maker mapped**, the competitive shortlist known, and the buyer **multi-threaded** (more than one
  contact). Discovery has real depth.
- **MEDDPICC bar:** `identify_pain` at least partial→confirmed; begin `economic_buyer`,
  `metrics`, `competition`.
- **Next milestone to drive toward:** discovery depth → demo → RFI/RFP positioning → multi-thread
  → map EB + DM. Salesforce marks entry with `Qualified_Submission_Date__c`.

### Stage 2 — Formal Evaluation

- **Meaning:** the buyer is formally evaluating vendors; the RFP round is live or imminent.
- **What you'll encounter:** **RFP round** (AI capabilities are scored here — SF
  `AI_Needs_in_RFP_Rating__c`; e.g. "Merlin Intake + MS Teams integration called out in the RFP"),
  RFI responses, structured demos, the start of **down-selection**. Buyer silence *during RFP
  drafting is process cadence, not a slip* — shape the evaluation criteria, don't chase for status.
- **Ideal / "good":** a strong, differentiated RFP response tied to the buyer's stated criteria; a
  developing champion; EB identified even if not yet engaged; you're being carried into the
  shortlist.
- **MEDDPICC bar:** `decision_criteria` + `decision_process` taking shape; `champion` emerging.
- **Next milestone:** RFI/RFP positioning, multi-thread, map EB + DM. Entry:
  `Formal_Eval_Submission_Date__c`.

### Stage 3 — Shortlisted

- **Meaning:** you're in the final set (often "the final two/three"). Being down-selected to the
  final N is authoritative progress; an eliminated incumbent is marked out with its date.
- **What you'll encounter:** **ShoeFit / BRD fit-gap sessions** (weed out misfits against the
  buyer's Business Requirements Document — SF `Shoe_Fit_Criteria_Met__c`, `Business_Requirements__c`),
  **deeper / use-case demos** (custom-tailored demos win deals — e.g. a custom financial-savings app
  demonstrated), securing a champion, and **booking the half/full-day workshop** (optional).
- **Ideal / "good":** ShoeFit criteria met against the BRD, use-case demos validated, **a secured
  champion with genuine access to power**, the workshop booked, no rival ahead.
- **MEDDPICC bar:** `champion` confirmed with access; `competition` mapped and you're not behind;
  `decision_criteria` confirmed.
- **Next milestone:** ShoeFit/BRD fit → deeper + use-case demos → secure the champion → book the
  workshop. Entry: `Shortlisted_Submission_Date__c`.

### Stage 4 — Vendor Selected

- **Meaning:** the buyer has chosen Zycus. The ceiling lifts to 100; the hard stage itself proves
  access to power. Contracting motion should now be *hot*.
- **What you'll encounter:** **commercials & pricing** open, the **ROI workshop**, MSA/SOW kickoff,
  securing **EB sponsorship**, the proposal to the CFO/C-level, **reference-customer calls**, and a
  **Horizon** (Zycus event) pull-in.
- **Ideal / "good":** an EB actively sponsoring, ROI quantified in a workshop, pricing framed, the
  paper process kicked off, references lined up. Momentum should read as one of the hottest deals
  in the book.
- **MEDDPICC bar:** `economic_buyer` confirmed/engaged, `metrics` (business case) quantified,
  `paper_process` starting.
- **Next milestone:** commercials + pricing → ROI workshop → MSA/SoW kickoff → EB sponsorship.

### Stage 5 — Negotiation *(a.k.a. Validation)*

- **Meaning:** commercial and legal terms are being worked; the deal is late-stage.
- **What you'll encounter:** **multi-round negotiation** across pricing permutations — **term
  length, services alongside SaaS, configuration / custom development, AI / Merlin integration
  touchpoints, credits, partner involvement** — plus **redlining** and **EB/CFO sign-off**.
- **Ideal / "good":** a mutual close plan, pricing converging, redlines progressing, EB/CFO sign-off
  secured or imminent. The only legitimate risks at this tier are date slippage, legal/paperwork,
  procurement/signature, budget pulled, or a *live* multi-vendor fight — a "missing champion/pain"
  is **not** a risk this late.
- **MEDDPICC bar:** `paper_process` active; `decision_process` confirmed.
- **Next milestone:** pricing permutations → redlining → EB/CFO sign-off.

### Stage 6 — Contract In Progress

**This stage is NOT atomic** — it holds **four independent tracks that resolve separately**. When
a Contract-In-Progress deal stalls, name *which* track; don't read it as generic stalling.

1. **Legal** — MSA redlines, jurisdiction, termination-for-convenience (T4C), board resolution.
2. **InfoSec / compliance** — SOC 1/2 + security questionnaire, DPA / GDPR+TOM (incl. the
   Zycus-India sub-processor disclosure), and — new for AI-module deals — an **AI-governance /
   AIGC board**. Runs **in parallel** with legal.
3. **Supplier onboarding** — vendor registration / risk portals (Aravo, Venminder) so the buyer's
   PO desk can issue a PO.
4. **Signature** — internal legal cover → e-sign (DocuSign / Certinal) → dual signatories.

- **What you'll encounter:** the InfoSec review, the **ERP / systems-integration deep-dive** (HLD/
  LLD scoping), SOW/MSA redlines, legal close.
- **Ideal / "good":** the **SOW is the choke point and the signature predictor** — buyers routinely
  agree the MSA + Order Form but **won't sign until the SOW is agreed** (signed separately by the
  AVP Global Delivery). "Won't sign until the SOW" is **normal**, not a red flag. Track SOW status
  to forecast the close; a quiet legal period is normal, not slipping.
- **Next milestone:** InfoSec review → ERP/systems-integration scope → SOW/MSA redlines → legal
  close.

### Stage 7 — Contract Signed → Stage 8 — PO Received

- **Contract Signed:** both parties have executed (RVP signs the MSA + Order Form 1; AVP Global
  Delivery signs the SOW). Delivery mobilization waits on the **signed SOW**.
- **PO Received:** **region-conditional.** DACH / APAC / emerging markets issue a PO that gates
  invoicing; **much of W. Europe and the US invoice directly with NO PO** (an "invoice details
  form"). A missing PO in Europe is **normal** — never flag "no PO" as a problem there. "PO
  Received" is a real SF stage but an **optional** gate to Closed-Won, not a universal one.
- **Handoff:** PO (where present) → internal **Zycus SO Form** → licence invoice; signed SOW →
  Phase-1 kickoff with Global Delivery + implementation partner.

---

## 3. The artifact & engagement catalog — everything you'll encounter, ranked by depth

The scoring engine ranks buyer-facing events by an **engagement-depth weight** (0–10): how much
each event *signals*. Use this both to recognize an artifact and to weigh it. Higher = deeper
buy-in. (From `deal_engine_footprints.py`.)

| Depth | Event / artifact | Typically appears | What it signals |
|---|---|---|---|
| **10.0** | **Proof of Concept (POC)** | Shortlisted → Vendor Selected | Deepest validation. A live POC with active buyer execution is strong *even with no commercials on the table*. |
| **9.0** | **Pilot** | Shortlisted → Vendor Selected | Hands-on production-like trial; near-decision. |
| **8.0** | **ROI workshop / procurement workshop / (customer) workshop** | Shortlisted → Vendor Selected | Buyer investing half/full days; value being co-built. Optional but a strong signal. |
| **7.5** | **Reference-customer call** | Vendor Selected (pre-sign) | Buyer wants a peer's word before signing off — a late-funnel buying signal. |
| **7.0** | **Reference / InfoSec / security review / legal review / redline / integration security** | Vendor Selected → Contract In Progress | Structural due-diligence; the buyer is spending real internal effort. |
| **6.0** | **Face-to-face / on-site / in-person; RFP / RFI** | Formal Evaluation → Shortlisted | Formal evaluation events; committee-level engagement. |
| **5.0** | **Deep-dive / detailed demo / technical / tech-alignment / integration / solution review** | Formal Evaluation → Shortlisted | Beyond a canned demo — real fit exploration. |
| **3.0** | **Standard demo / presentation / walkthrough** | Qualified → Formal Evaluation | Early interest; broad, not yet tailored. |
| **2.0** | **Kickoff** | post-signature | Delivery mobilization. |
| **1.5** | **Discovery / intro call** | Qualified | Top of funnel; establishing pain & players. |

**How to read the catalog:**
- **Demos are not one thing.** A depth-3 *standard demo* early ≠ a depth-5 *use-case/deep-dive
  demo* at Shortlisted. Name which.
- **A POC is a distinct motion, not a commercial close.** Read POC momentum as **validation →
  sign-off → expand**. What "good" looks like: **documented/agreed success criteria + active buyer
  execution + a POC sign-off**. A "POC successful / validated as best platform" note is a Zycus
  **win indicator** (and must be attributed to Zycus winning — never logged as a competitor's quote).
- **Reference calls are sequenced late** — after the EB proposal, usually just before POC/deal
  sign-off ("the buyer wanted to talk to a customer before signing off"). A rep's "reference call
  went well" is the *rep's* read — label it **rep-reported, not buyer-confirmed** unless the
  buyer's own feedback is captured.
- **InfoSec + integration deep-dive sit late** (just before contracting) and can gate signature.
- **Champion-building is continuous** and ranks *before* any commercial step when the champion is
  weak / developing / at-risk.

---

## 4. Motion types — not every deal is read the same way

The same stage can mean different things depending on the *motion*. Detect the motion, then read
engagement against its norms (from `mase_revops_head.md` + the scorer's process-mode).

- **Standard motion.** Multi-thread, build the business case, keep the committee warm. Silence is
  drag.
- **RFP / tender motion.** During RFP drafting, buyer silence is **process cadence, not a slip** —
  shape the evaluation criteria rather than chase for status. The scorer's **process-mode**
  recognizes this: at Formal Eval / Shortlisted / Vendor Selected with a live *future* milestone
  date and RFP/tender keywords present (and no pause signal), stalling drag is suspended and
  momentum is floored at 50. **Anti-zombie guard:** if the deadline has *passed in silence*,
  process-mode does **not** apply — that's a real stall.
- **Champion-authored tender.** Arm the champion to broker the EB meeting; don't cold-outreach the
  EB around them.
- **Workshop / POC-led motion.** Drive to **documented success criteria and a decision date**. A
  live POC near a placeholder close is *Close Date Risk* (healthy, date will slip), not *Slowing*.

**Process-milestone keywords the engine watches** (RFP/tender detector): `rfp, rfi, rfq, bafo,
tender, demo, orals, clarification, infosec, security review, legal review, redline, sow, proposal,
submission, due, award, decision, workshop, presentation, evaluation, down-select, pricing, cfo,
exco, steerco, board review/meeting`.
**Pause/stall keywords:** `postponed, on hold, hold until, budget freeze, re-baseline, next
quarter, paused, deferred, frozen, pushed to Q#/next`.

---

## 5. The contracting paper trail (post-shortlist) — the full document relay

Once you're Vendor Selected, contracting is a **hand-off relay across six phases**, spanning
`Vendor Selected → Negotiation → Contract In Progress → Contract Signed → PO Received`. (Full
reference: `docs/zycus-contracting-reference.md`.)

### The six phases

1. **Commercials locked** (Vendor Selected → Negotiation) — price, term, phasing agreed; buyer
   signals intent. Artifacts: **BAFO** issued, **LOI** received, 5-yr term / payment milestones.
2. **Paper drafted** (Negotiation → Contract In Progress) — *"whose template?"* Zycus SaaS paper
   vs buyer standard. Artifacts: **MSA** drafted, **Order Form 1 (+2)**, **SOW** authored.
3. **Legal & redlines** (Contract In Progress) — legal-to-legal on clauses; jurisdiction and
   **T4C** are the usual sticking points. Artifacts: MSA redlines, jurisdiction / board resolution.
4. **InfoSec, compliance & onboarding** (Contract In Progress · **parallel to legal**) — the
   silent gate on the PO. Artifacts: Security / RTO-RPO review, DPA / data compliance, vendor
   registration, AI-governance board.
5. **Signature** (Contract In Progress → Signed) — internal legal cover / audit trail → e-sign
   (DocuSign / Certinal) → dual signatories.
6. **PO & delivery handoff** (Contract Signed → PO Received) — PO unlocks invoicing; signed SOW
   unlocks delivery.

### The forcing functions (who is blocked until a document clears)

- **Signed SOW required** → Global Delivery + implementation partner cannot mobilize the Phase-1
  kickoff. **Signed separately by the AVP Global Delivery**, not bundled with the MSA. *The SOW is
  the universal choke point and the best signature predictor.*
- **PO required (region-dependent)** → Finance raises the internal **Zycus SO Form**, then the
  licence invoice. US/APAC/emerging: a buyer PO gates this. Much of Europe: **no PO** — Finance
  invoices directly.
- **Supplier onboarding required** → the buyer's PO desk can't issue a PO to a vendor not in the
  supplier master — increasingly via a risk portal (Aravo, Venminder) that can itself stall. Submit
  trade licence, TRN, tax forms, bank details early.
- **InfoSec / vendor-risk sign-off** → can hold signature outright: SOC 1 / SOC 2 + security /
  technical / governance questionnaires; elsewhere surfaces as RTO/RPO and integration-standard
  conformance.
- **Data privacy + jurisdiction** → Legal / DPO (and sometimes the board) clear jurisdiction, the
  DPA / GDPR+TOM addendum (incl. Zycus-India sub-processor disclosure) and term-length policy.
- **AI-governance approval** → new for AI-module deals: an AI-compatibility / governance board
  clears the platform before signature (growing as Zycus leads with Merlin / Agentic AI).
- **Order Form 2 (e-sign)** → where the customer adopts Zycus **Certinal** for signing, its own
  Order Form must execute to stand up the signing platform.

### Document glossary (artifact → owners → what it gates)

| Artifact | Zycus ↔ buyer owners | Gates |
|---|---|---|
| **BAFO** (Best & Final Offer) | Deal Desk/Sales ↔ Procurement | Locks commercials; precedes LOI |
| **LOI** (Letter of Intent) | Sales ↔ Procurement | Buyer intent → unlocks paper drafting |
| **MSA** (Master Service Agreement) | Legal ↔ Legal/Risk | Master legal terms; the redline battleground |
| **Order Form 1** | Deal Desk ↔ Procurement | Products, pricing, spend basis; signed with the MSA |
| **Order Form 2** | Deal Desk / Certinal ↔ Procurement | Add-on / product-specific (e.g. Certinal e-sign) |
| **SOW** (Statement of Work) | Global Delivery (AVP) ↔ Procurement/IT | **The universal choke point**; signed separately by AVP Delivery |
| **Framework + Call-Off** | Legal/Deal Desk ↔ Procurement | Nordics alternative to MSA+OF |
| **SOC 1 / SOC 2 + questionnaire** | Zycus Security/Delivery ↔ Vendor Risk/InfoSec | Hard pre-signature gate |
| **NDA** | Sales ↔ Procurement | Clears info exchange for kickoff |
| **Supplier onboarding** | Sales ↔ Vendor Mgmt | Gates PO issuance |
| **DPA + GDPR / TOM addendum** | Legal ↔ Legal/Risk/DPO | Europe/US signed; discloses Zycus-India sub-processor |
| **AI-governance approval** | Deal team ↔ AI Governance/Risk | Buyer AI board clears the platform |
| **InfoSec / security review** | Delivery ↔ IT Architecture/InfoSec | RTO-RPO, pen-test, integration standard; can tie to SOW signature |
| **Compliance / jurisdiction** | Legal ↔ Legal/Risk + Board | Enforceable jurisdiction + term/termination; can need a board resolution |
| **e-signature** | both sign | Dual signatories (RVP + AVP Delivery) |
| **Zycus SO Form** (internal sales order) | Sales → Finance | Internal booking; hands the won deal to Finance for invoicing |
| **PO** (Purchase Order) | Sales/Finance ↔ Finance/Proc | Unlocks invoice — **often absent in Europe** |

**Signatories:** the **RVP** signs the MSA + Order Form 1; the **AVP Global Delivery** signs the SOW.

### Region & deal-type flex (the sequence holds; the paper set flexes)

- **US** — NDA-first; InfoSec / pen-test the main slip; often **no PO** (direct SaaS invoice).
- **W. Europe** — heaviest privacy stack (DPA, GDPR+TOM, sub-processor disclosure, AI-governance);
  usually **no PO**.
- **DACH** — disciplined PO → Sales Order.
- **APAC & emerging** — PO present but trails signature; often gated by a supplier-risk portal (Aravo).
- **Nordics** — can shortcut via **framework + call-off** (no MSA+OF).
- **Single-module (Certinal-only)** — Order Form + SOW, **no MSA**; lighter — do not weight as
  full-suite.

---

## 6. MEDDPICC reference (the qualification backbone)

The engine reads **8 MEDDPICC elements**, each with a status of **`confirmed` | `partial` | `gap`**,
sourced from these Salesforce fields (`MEDDPICC__c` preferred over `MEDDPICC_2_0__c`):

| Element | Salesforce source field | Win factor it feeds |
|---|---|---|
| **Metrics** | `Metrics_Important_to_Buyer__c` | business_case |
| **Economic buyer** | `Who_is_the_economic_buyer__c` (+ budget owner `Who_Own_s_the_budget__c`, budget `What_is_the_budget__c`) | exec_access |
| **Decision criteria** | `Decision_Criteria__c` | (criteria) |
| **Decision process** | `Purchase_Process__c` | commercial |
| **Paper process** | *(paper process)* | commercial |
| **Identify pain** | `What_problem_is_Zycus_solving__c` | differentiation |
| **Champion** | `Champion_for_Zycus__c` | champion |
| **Competition** | `Competition_and_our_differentiator__c` | competitive |

Related SF fields: **Blockers** `Any_blockers__c`, **Products considered** `Products_being_considered__c`,
**ShoeFit** `Shoe_Fit_Criteria_Met__c`, **BRD** `Business_Requirements__c`, **RFP AI rating**
`AI_Needs_in_RFP_Rating__c`, stage-entry dates `Qualified_Submission_Date__c` /
`Formal_Eval_Submission_Date__c` / `Shortlisted_Submission_Date__c`.

**Missing evidence is a mild negative, never neutral** — "we haven't proven it yet" chips the score
down rather than being ignored.

---

## 7. How deal health is judged (verdict rails + qualification gates)

### The four verdict states (grade against the close date + buyer engagement)

- **On Track** — significant recent movement consistent with the stage, buyer engaged on the next
  step, close date still credible.
- **Close Date Risk** — genuinely healthy and engaged, but the remaining steps can't complete by
  the forecast date, so the date will slip. *A positive, light read — the deal is good, the date is
  optimistic* (e.g. a live POC 5 days from a placeholder close).
- **Slowing** — one key action stalled (waiting on an approval / missing info) OR engagement
  thinning, but not yet cold.
- **Off Track** — no buyer-facing deliverable *and* no engagement in 60 days. Cold 60+ days is
  forced Off Track regardless of stage.
- **Precedence:** Off Track > Slowing > Close Date Risk > On Track. LATE-stage deals may only be On
  Track or Close Date Risk (a quiet legal period is normal, never Off Track).

### The deal pulse (server-computed, authoritative)

Every deal carries a today-anchored engagement state — **live / cooling / dark** — from Salesforce
LastActivityDate, the buyer calls actually read, days-in-stage, close proximity, forecast, and any
dated rep outreach. Every narrative (verdict, risks, moves) must tell **one consistent story** that
matches the pulse. A dated rep outreach = "rep reached out, awaiting buyer reply" (a rep touch, not
buyer engagement).

### The qualification gates on Win (why a healthy-looking deal can still be capped)

A high Win probability must be **earned by ticking qualification boxes**, not inferred from
enthusiasm. **Access to Power dominates:** Win is capped at the *minimum* of these gates —

| Gate | confirmed | partial | gap / missing |
|---|---|---|---|
| **Economic buyer** (Access to Power) | 100 | 74 | **52** / 50 |
| **Competitive visibility** | 100 | 90 | 66 |
| **Champion** | 100 | 86 | 60 / 58 |

- **Post-selection stages** (Vendor Selected → PO) lift the cap to 100 — the hard SF stage itself
  proves access.
- **Selection override:** a confirmed selection whose CRM stage lags is anchored to 72 with the 100
  ceiling unlocked — but only with a **confirmed EB**, a non-slowing verdict, high preference, a
  positive competitive edge, and a real won/Commit signal. Inference alone never crosses the ceiling.
- **Relationship leverage (+10):** if the account has a sibling Closed-Won or a strong live sibling
  deal (advanced stage / Commit / Best Case), the deal gets a foothold credit — *we're already in.*
  (Capped by the deal's own stage ceiling.)

---

## 8. What Zycus sells (so module names in a deal are legible)

- **Source-to-Pay (S2P)** — the full end-to-end suite (sourcing + contracts + supplier + P2P) on
  one platform; the "single integrated platform" wedge.
- **Source-to-Contract (S2C)** — upstream subset (spend + sourcing + contracts + supplier), no P2P.
- **iSource (eSourcing)** — strategic sourcing, RFx, e-auctions.
- **iContract (CLM)** — contract lifecycle management (authoring, repository, AI contract search).
- **iSupplier (SRM) / ZSN** — supplier management & performance; Zycus Supplier Network.
- **iRisk / iRisk Lite** — supplier risk (ESG/compliance).
- **iAnalyze (Spend Analysis) + AutoClass** — **spend analytics** & auto-classification.
- **iSave / iManage** — savings management / supplier management (older ML-branded upstream trio).
- **iRequest / Merlin Intake** — requisition/intake; Merlin Intake is the modern intake experience
  (positioned vs Zip) with an S2P expansion path.
- **eProcurement (eProc) + eInvoice** — P2P, catalogs, AP/invoice automation.
- **Merlin AI / Merlin Agentic AI** — the AI layer (launched 2018; "6+ years" maturity); Merlin
  Studio, Merlin Intake.
- **ANA (Autonomous Negotiation Agents)** — Merlin Agentic AI negotiation module (launched Feb
  2025). *(verify module scope — do not confuse with spend analytics, which is iAnalyze.)*
- **iSaaS** — the single integration gateway (file + API, no middleware).
- **Certinal** — Zycus e-signature.
- **iConsole** — executive dashboard. **AppXtend / AppX** — low-code composable app store +
  connectors. **iMaster / TMS** — vendor master data & user/tenant management.

---

## 9. Implementation & delivery (post-signature)

- **Formal implementation phase names:** **not in the knowledge base** — the
  `Zycus Implementation Framework` deck is UNAVAILABLE (see §11). The closest available lifecycle is
  the post-sale success lifecycle (below).
- **iSwitch change management** — five phases: **Change-Management Strategy → Communication →
  Training → Rollout → Feedback Loop.** Two models: **iSwitch Communication** (lighter) and
  **iSwitch Local** (full CM for one rollout). **Kickoff mechanics:** RACI finalized in the first
  meeting; customer assigns a **Single Point of Contact (SPC)**; requirements gathered per sprint.
  **Training:** Train-the-Trainer via **Zycus University**; Zycus certifies champions. Scope metrics:
  Pilot = 20 champions + 100 suppliers; per rollout = 20 champions + 400 suppliers. **Separately
  scoped & priced;** English-only unless agreed.
- **Integration** — **iSaaS** is the single gateway for all modules; **file-based** (XML/SFTP) and
  **API-based** (JSON/REST), **no middleware**. 1,000+ APIs. Auth: OAuth 2.0 / 2FA / mSSL. **SSO:**
  ADFS, Ping, ForgeRock, SiteMinder, Okta, Azure AD, IBM TIVOLI, OneLogin, NetIQ. User provisioning
  via TMS APIs against customer HR. **SAP S/4HANA — two paths:** (1) SAP CI/BTP-certified adapter
  (runs on the customer's SAP BTP, no extra middleware) and (2) iSaaS adapter (any system); "80%
  out-of-the-box"; jointly maintained by Rojo Consultancy + Zycus. **Delivery docs:** HLD, LLD, SIT
  test reports, post-go-live maintenance; RACI splits Zycus vs customer.
- **Customer-success lifecycle (TAM/CAM):** **Design → Onboarding → Go-Live → Value Sustenance →
  Value Realization → Value Expansion.**

---

## 10. Support model & competitive landscape

### TAM / CAM support model

- **CAM (Customer Account Manager)** — relationship / adoption / ROI; runs Quarterly Business
  Reviews & Steering Committee; owns the Success Plan, contract mgmt, change requests.
- **TAM (Technical Account Manager)** — technical delivery; Monthly Business Reviews, roadmaps,
  usage/KPI/ROI, hypercare. *(Dedicated TAM is Premium-tier only.)*
- **Support tiers:** **Professional** (included; 24×7 Sev-1, 9×5 other, shared services) →
  **Enterprise** (designated analyst, Sev-1+2 incident mgmt, quarterly success) → **Premium**
  (dedicated analyst + designated TAM, Sev-1+2+3, monthly success, value-realization + CXO
  Leadership Connect 2×/yr). *(No public pricing / SLA response-time figures — do not cite.)*

### Competitive positioning *(all win stories/stats are **(verify)** — internal positioning, not customer-citable without reference approval)*

- **Coupa** — PE-owned scale player, Gartner Leader. Zycus wins on: integrated single suite, Merlin
  AI maturity, supplier experience, TCO, UX, support (vs "chat-only"). Avoid: market share, analyst
  position, implementation-speed reputation.
- **GEP** — software + consulting + managed services, slow (6–24 mo), $500K+. Zycus wins on:
  pure-play SaaS, faster deploy (3–6 mo), UX, transparent/mid-market pricing, non-Microsoft-centric.
  Avoid: Fortune-500 count, managed-services depth.
- **Ivalua** — Gartner Leader, low-code flexibility, 98% retention. Zycus wins on: faster
  integration, autonomous/agentic AI (Merlin ANA vs IVA), lower entry, ease of use. Avoid: Leader
  status, retention stat.
- **Jaggaer** — PE-owned, strong in manufacturing/public sector/direct spend. Zycus wins on: UX,
  faster implementation, **AI maturity ("7-year head start")**, TCO, transparent pricing. Avoid:
  PE-exit-fear language.
- **SAP Ariba** — 29.4% market share, SAP ecosystem lock-in, Joule AI (2024). Zycus wins on: UX/
  adoption, Merlin maturity (6+ yrs), **non-SAP/multi-ERP integration with no middleware** (backed
  by the S/4HANA deck), TCO, mid-market fit, faster implementation. Avoid: supplier-network size,
  SAP-native integration, raw scale.
- **Zip** — intake/orchestration challenger, fast (7-week) deploys, modern UX, mid-market/US. Zycus
  wins on: full-S2P depth (Zip lacks strategic sourcing, CLM, supplier risk), single-vendor
  accountability, global scale, Merlin Intake as the equivalent intake layer + S2P path. Exposed on:
  deploy-speed perception, low initial cost, modern-UX narrative. **No Zip win stories exist — do
  not fabricate any.**

---

## 11. Caveats, gaps & how to use this responsibly

- **Never speak the score machinery in a human-facing reason.** The stage ceilings, anchors,
  momentum lifts and qualification caps here are *internal calibration*. Explain a deal with its own
  facts ("no economic buyer is engaged and the field is still narrowing to two"), never "the
  Shortlisted cap holds it at 70."
- **`(verify)` = unconfirmed.** Every competitive stat/win story and every do-not item from the
  knowledge cards must be checked against a live source (Salesforce for references, Showpad/
  commercial team for figures/pricing) before customer-facing use.
- **Known gaps:** the **Zycus Implementation Framework** deck is UNAVAILABLE (so the formal
  implementation *phase names* are not in this KB — human review of the original deck required); the
  **iSaaS datasheet** is password-encrypted (its facts here are sourced from the Integration
  Capabilities + SAP S/4HANA decks instead); **pricing is deliberately withheld** everywhere.
- **New-business scope.** Renewals / change-requests / cross-sell / upsell and Certinal
  annual-invoicing are lighter paths — don't weight them as full-suite new-business deals.

---

### Source map

| Section | Primary source |
|---|---|
| Enterprise motion, stage→milestone, verdict rails, deal pulse, reading rules | Live `mase_deal_sweep` Supabase prompt (§4, §3, §2.10) |
| Per-stage numbers, engagement-depth ladder, cadence, MEDDPICC, qualification gates, process-mode | `deal_engine_scoring.py`, `deal_engine_footprints.py`, `deal_engine_verdict.py` |
| Contracting 6-phase relay, document glossary, forcing functions, region flex | `docs/zycus-contracting-reference.md` (+ live prompt §2.9) |
| Products, implementation (iSwitch/integration/SAP), TAM-CAM, support tiers, competitive | `knowledge_index/` Showpad cards |

*Maintained as MASE domain knowledge. When deal behaviour or the engine calibration changes, update
this file and note it in `CHANGELOG.md`.*
