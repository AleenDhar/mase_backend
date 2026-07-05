# Zycus — New-Business Contracting & Sales-Stage Reference (MASE domain knowledge)

> Source: Zycus sales-ops reference, built from 17 new-business deals (15 Closed-Won,
> trailing 12 mo, + Mair & Omnia in final contracting), 12 countries, ~20 document
> artifacts, 14 dependent teams. Salesforce + DeepAgent, swept Jun–Jul 2026.
>
> **Scope: NEW-BUSINESS deals only.** Renewals, change-requests, cross-sells, upsells
> and Certinal annual-invoicing follow a lighter path and are excluded.
>
> This is the canonical glossary of how Zycus goes from "we won" to "we can deliver."
> The distilled version lives in the `mase_deal_sweep` Supabase prompt so every sweep
> interprets contracting-stage deals with it; this file is the full reference.

## The six contracting phases → Salesforce stages

Contracting is a **hand-off relay**, not one stage. The whole span sits inside
`Vendor Selected → Negotiation → Contract In Progress → Contract Signed → PO Received`.

1. **Commercials locked** (Vendor Selected → Negotiation) — price, term, phasing agreed;
   buyer signals intent. Artifacts: **BAFO** issued, **LOI** received, 5-yr term / payment milestones.
2. **Paper drafted** (Negotiation → Contract In Progress) — "whose template?" Zycus SaaS
   paper vs buyer standard. Artifacts: **MSA** drafted, **Order Form 1 (+2)**, **SOW** authored.
3. **Legal & redlines** (Contract In Progress) — legal-to-legal on clauses; jurisdiction &
   termination-for-convenience (T4C) the usual sticking points. Artifacts: MSA redlines,
   jurisdiction / board resolution, T4C.
4. **Infosec, compliance & onboarding** (Contract In Progress · **PARALLEL** to legal) —
   the silent gate on the PO. Artifacts: Security / RTO-RPO review, DPA / data compliance,
   vendor registration.
5. **Signature** (Contract In Progress → Signed) — internal legal cover / audit trail →
   e-sign (DocuSign / Certinal) → dual signatories.
6. **PO & delivery handoff** (Contract Signed → PO Received) — PO unlocks invoicing; signed
   SOW unlocks delivery. "PO Received" is its own SF stage.

## Who is blocked until a document clears (the real forcing functions)

- **Signed SOW required** → Global Delivery + Implementation Partner cannot mobilise the
  Phase-1 kickoff. The SOW is signed **separately by the AVP Global Delivery** — not bundled with the MSA.
- **PO required (region-dependent)** → Finance raises the internal Sales Order ("Zycus SO
  Form") then the licence invoice. US/APAC/emerging: a buyer PO gates this. Much of Europe:
  **no PO** — Finance invoices directly off an "invoice details form."
- **Supplier onboarding required** → the buyer's PO desk can't issue a PO to a vendor not in
  the supplier master — increasingly via a risk portal (Aravo, Venminder) that can itself
  stall. Submit trade licence, TRN, tax forms, bank details early.
- **Infosec / vendor-risk sign-off** → Vendor Risk / InfoSec can hold signature outright:
  SOC 1 / SOC 2 + security/technical/governance questionnaires. Elsewhere surfaces as
  RTO/RPO and integration-standard conformance (a post-signature design risk if skipped).
- **Data privacy + jurisdiction** → Legal / DPO (and sometimes the board) must clear
  jurisdiction, the DPA / GDPR+TOM addendum (incl. Zycus-India sub-processor disclosure)
  and term-length policy. A board resolution can override the negotiated position.
- **AI-governance approval** → new for AI-module deals: an AI-compatibility / governance
  board must clear the platform before signature (Swift's "AIGC"). Growing as Zycus leads
  with Merlin / Agentic AI.
- **Order Form 2 (e-sign)** → where the customer adopts Zycus Certinal for signing, its own
  Order Form must be executed to stand up the signing platform.

## Document glossary (artifact → owner Zycus↔buyer → what it gates)

- **BAFO** (Best & Final Offer) — Deal Desk/Sales ↔ Procurement — locks commercials; precedes LOI.
- **LOI** (Letter of Intent) — Sales ↔ Procurement — buyer intent → unlocks paper drafting.
- **MSA** (Master Service Agreement) — Legal (Zoheb) ↔ Legal/Risk — master legal terms; the redline battleground. (Zycus paper won on both reference deals.)
- **Order Form 1** — Deal Desk ↔ Procurement — products, pricing, spend basis; signed with the MSA.
- **Order Form 2** — Deal Desk / Certinal ↔ Procurement — add-on / product-specific (e.g. Certinal e-sign order).
- **SOW** (Statement of Work) — Global Delivery (AVP) ↔ Procurement / IT — **the universal choke point**; buyers agree MSA+OF but won't sign until the SOW is agreed. Signed separately by AVP Delivery.
- **Framework + Call-Off** — Legal / Deal Desk ↔ Procurement — Nordics alternative to MSA+OF: an order "called off" a pre-agreed framework.
- **SOC 1 / SOC 2 + security questionnaire** — Zycus Security/Delivery ↔ Vendor Risk/InfoSec — hard pre-signature gate.
- **NDA** — Sales ↔ Procurement — buyer-issued; clears info exchange for kickoff.
- **Supplier onboarding** — Sales supplies ↔ Vendor Mgmt — gates PO issuance (trade licence, TRN, bank details, tax forms; often a risk portal).
- **DPA + GDPR / TOM addendum** — Legal ↔ Legal/Risk/DPO — Europe/US signed artifact; discloses sub-processor (Zycus India) + sanctions screening.
- **AI-governance approval** — Deal team ↔ AI Governance/Risk — buyer AI board clears the platform (AI-module deals).
- **Infosec / security review** — Delivery ↔ IT Architecture/Infosec — RTO-RPO, pen-test, integration standard; can tie to SOW signature.
- **Compliance / jurisdiction** — Legal ↔ Legal/Risk + Board — enforceable jurisdiction + term/termination policy; can need a board resolution.
- **Internal legal cover** — buyer-internal sign-off before external e-sign.
- **e-signature** — both sign; dual signatories (RVP + AVP Delivery).
- **Zycus SO Form** (internal sales order) — Sales → Finance — internal booking; hands the won deal to Finance for invoicing.
- **PO** (Purchase Order) — Sales/Finance ↔ Finance/Proc — unlocks invoice — **often absent in Europe** (direct SaaS invoice).

## Region & deal-type shifts the load (the sequence holds; the paper set flexes)

- **US** — NDA-first; InfoSec / pen-test the main slip; often **no PO** (direct SaaS invoice).
- **W. Europe** — heaviest privacy stack (DPA, GDPR+TOM, sub-processor disclosure, AI-governance boards); usually **no PO**.
- **DACH** — disciplined PO → Sales Order.
- **APAC & emerging markets** — PO present but trails signature; often gated by a supplier-risk portal (Aravo).
- **Nordics** — can shortcut via framework + call-off.
- **Single-module (Certinal-only)** — Order Form + SOW, **no MSA**; lighter — shouldn't carry full-suite stage weight.

## Three takeaways for MASE stage modelling

1. **Contract In Progress is NOT atomic** — it holds a legal track, an infosec/compliance
   track, a supplier-onboarding track and a signature track that resolve independently.
   Sub-stage them so "in contracting" is told apart from "blocked on one gate."
2. **The SOW is the signature predictor, not the MSA** — track SOW status to forecast the close.
3. **"PO Received" is region-conditional** — reliable in DACH/APAC/emerging, but much of
   Europe invoices directly with no PO, so a PO stage must be **optional**, not a required gate to Closed-Won.
