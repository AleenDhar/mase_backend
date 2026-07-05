---
name: zycus-contracting-glossary
description: "Zycus new-business contracting paper-trail + terminology the analysis must understand (SOW = choke point, Contract-In-Progress = 4 parallel gates, PO region-conditional)."
---

# Zycus contracting & terminology (new-business deals)

Full reference: [docs/zycus-contracting-reference.md](../../docs/zycus-contracting-reference.md).
Distilled version is appended to the `mase_deal_sweep` Supabase prompt so every sweep reads
contracting-stage deals correctly.

**Why.** The system needs to understand Zycus's terms and how we operate from "won" to
delivery, so late-stage/contracting deals aren't mis-read (e.g. treating a normal
"won't sign until SOW" or a Europe "no PO" as a red flag, or reading Contract In Progress
as one gate when it's four).

**Key facts the analysis must apply:**
- Contracting = 6 phases inside SF stages `Vendor Selected → Negotiation → Contract In
  Progress → Contract Signed → PO Received`.
- **Contract In Progress is NOT atomic** — four INDEPENDENT tracks resolve separately:
  legal (MSA redlines, jurisdiction/T4C), infosec/compliance (SOC 1/2, DPA/GDPR+TOM,
  AI-governance board), supplier-onboarding (vendor registration / risk portals like
  Aravo/Venminder), signature. When a Contract-In-Progress deal stalls, name WHICH gate.
- **The SOW is the choke point + signature predictor** — buyers agree the MSA + Order Form
  but WON'T SIGN until the SOW closes (signed separately by AVP Delivery). "Won't sign until
  SOW" is NORMAL; track SOW status to forecast the close.
- **PO is region-conditional** — DACH/APAC/emerging issue a PO (gates invoicing); much of
  W. Europe/US invoices directly with NO PO. A missing PO in Europe is NORMAL, not a problem.
- **New-business only.** Renewals/change-requests/cross-sell/upsell are lighter.
  Single-module (Certinal-only) = Order Form + SOW, no MSA — don't weight as full-suite.
- **Glossary:** BAFO (Best & Final Offer), LOI (Letter of Intent), MSA (Master Service
  Agreement), OF1/OF2 (Order Form — primary / add-on e.g. Certinal), SOW (Statement of Work),
  DPA / GDPR+TOM (data-processing, incl. Zycus-India sub-processor), SOC 1/2 (infosec
  evidence), T4C (termination-for-convenience), framework + call-off (Nordics alt to MSA+OF),
  Zycus SO Form (internal sales order → Finance), AIGC / AI-governance board (buyer AI board),
  RTO/RPO (recovery-time/point objectives). Signatories: RVP signs MSA+OF1, AVP Delivery signs SOW.

**How to work with it.** Distilled block appended to the Supabase `mase_deal_sweep` prompt
(backup saved outside the repo). To also give the deal-chat / CEO-attention agents this
knowledge, append the same block to `mase_chat_agent` / the judge prompt. Code-level
stage-modelling (sub-stage Contract-In-Progress, SOW-status close predictor, PO optional)
is a larger follow-up, not done yet.
