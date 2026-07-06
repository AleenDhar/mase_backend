# MASE - Forecasted-Deal To-Dos

Snapshot: 2026-06-24  |  Source: `GET /api/deal-engine/todo` (`deal_engine_store.derive_todo`)  |  Scope: `forecast_category` in Commit / Best Case / Upside Key Deal  |  54 deals, 366 to-dos

## 1. How to-dos are stored in the DB

**There is no standalone `todos` table.** To-dos are **derived at read time** by `deal_engine_store.derive_todo()` and served by `GET /api/deal-engine/todo`.

### Source of truth - `deal_records` (Supabase project `wfwgatyfzqzrcauatufb`, `record` JSON column)
One row per opportunity. To-dos come from five fields under `record.ai`:

| Category | Source `record.ai.*` | Item text | Filter in derive_todo |
|---|---|---|---|
| Critical move | `recommended_moves` (ranked; key/forecast-critical deals) | `action` | near-term horizon (`act_by`/`trigger_date`); cap `TODO_MAX_CRITICAL` |
| Open commitment | `open_deliverables` | `commitment` | status open/overdue, within horizon + recency |
| Explicit requirement | `explicit_requirements` | `requirement` | not `addressed`, within recency |
| Implicit need | `implicit_requirements` | `inferred_need` | within recency |
| Best-practice flag | `best_practice_check.flags` | `flag` | dropped if it contradicts a live pulse; cap `TODO_MAX_BEST_PRACTICE` |

Each item also carries `opp_id, account_name, opp_name, owner_name`, dates (`due`/`act_by`/`date`), `urgency`, and extras (`who`, `intervention_owner`, `expected_effect`, `grounding_quote`, `said_by`).

### Stable identity - `todo_key`
Every item gets a deterministic `todo_key` (`_stamp_todo`) so edits, deletes, and push-state stay attached to the same logical to-do across the daily re-sweep.

### Overlay tables (same Supabase project)
- **`deal_todo_overrides`** - user edits/deletes, keyed by `todo_key` (a deleted to-do stays gone; an edited one keeps its wording + due date).
- **`deal_todo_pushes`** - ledger of to-dos pushed to Salesforce as completed Tasks (`todo_key -> sf_task_id`); drives the "pushed" badge. Push is a direct server-side simple-salesforce write (respects the SF write lockdown).
- Manual completed updates are a separate source merged into "Recently completed."

### Read path
`GET /api/deal-engine/todo?owner=` -> `derive_todo(owner)` -> `{ owner, critical[], important[], explicit[], implicit[], best_practice[], manual[] }`. The frontend (`useBackendTodos` -> `bucketsForOpp`) buckets per opp. Note: the **clubbing** step (`todo_grouping.group_todo_lists`) runs at sweep/persist time and collapses near-duplicate commitments/flags into one per theme **before** storage, so this list is already de-duplicated.

## 2. To-do list by forecasted deal

### AAK  -  AAK - eProc/Merlin Intake Mar'26
_Owner: Justin Ajmo  |  opp_id: 006P700000UNHh5_

**Critical moves** (5)
- Re-engage Tony immediately with business-case co-creation offer: schedule a working session within 7 days to help quantify baseline metrics (requisition volume, PO cycle time, manual-process cost per req) and model ROI from catalog enforcement (tail-spend reduction), policy compliance (approval-cycle savings), and spend-data accuracy improvements (80% to 95%+). Position as strategic partner helping Tony build the credible leadership case he stated he lacks, not a vendor waiting for RFP. This restarts momentum, differentiates from competitors sitting idle, and creates obligation/reciprocity.  _(by 2026-07-01; owner: Deal team)_
- Secure Economic Buyer introduction and meeting within 14 days: ask Tony directly to introduce the global procurement excellence director (Swedish HQ, controls 2027-2030 eProcurement strategy per Tony 2 Mar) and the US budget authority (CFO or finance leader) so Zycus can present the co-created ROI case, demonstrate alignment with global roadmap, and validate interim-solution positioning. Frame as 'we want to make sure our solution fits both your immediate US needs and your global team's long-term strategy—can we schedule 30 minutes with to align on that?'  _(by 2026-07-08; owner: Deal team)_
- Multi-thread by activating demo attendees within 14 days: reach out directly to Yvonne Lawson (AP Specialist), Michael Crockett (Senior Buyer), and Nigel Glover (Director of Engineering/Ops) with tailored value propositions—AP automation ROI and 3-way-match efficiency for Yvonne, MRO catalog setup and Grainger punch-out simplification for Michael, plant-level procurement policy enforcement and spend visibility for Nigel. Offer individual 20-minute follow-up calls to address their specific pain points and convert at least one to an internal advocate who validates Zycus and pressure-tests competitors.  _(by 2026-07-08; owner: Deal team)_
- Close open deliverables and re-confirm fit within 14 days: (1) confirm service rate-card upload capability (Tony's 25 Mar question) and provide example; (2) clarify professional-services charge for SAP migration (Tony's 25 Mar question); (3) send Merlin Intake vs. eProcurement summary doc (committed 25 Mar). Package all three into a single email with subject 'AAK eProcurement Eval: Closing Open Items' and use it as a re-engagement hook to request a follow-up call to discuss next steps and RFP timeline.  _(by 2026-07-08; owner: Deal team)_
- If no buyer reply within 10 business days of rank-1 re-engagement attempt, escalate to executive connect: the deal owner's manager reaches out to Tony and, if identified, the global procurement excellence director with a senior-to-senior message: 'We understand AAK is evaluating eProcurement solutions and want to ensure we're aligned on both your immediate US needs and your global 2027-2030 strategy. Can we schedule a brief executive alignment call to discuss how Zycus fits into your roadmap?' Position as partnership discussion, not sales push.  _(by 2026-07-15; owner: Executive connect)_

**Open commitments** (1)
- Tony to create requirements document for multi-vendor eval and share RFP timeline  _(who: Buyer; open)_

### ACEN  -  ACEN S2P
_Owner: Carl Kimball  |  opp_id: 006P700000DkWgX_

**Critical moves** (5)
- Schedule and deliver CFO-led ROI workshop with the Economic Buyer (CFO) and the champion (President) to quantify savings from Zycus vs in-house P2P system, reframe SAP middleware cost as TCO advantage (Zycus + integration fee is less than SAP Ariba total cost + ERP lock-in), and secure written business case approval to unblock IT steering committee decision. Anchor the ROI model to the 70% non-PO payment manual disconnect pain and the procurement digitization mandate. Position Merlin AI (agentic negotiation, GenAI chatbot, AI-powered invoice OCR) as the centerpiece of the TCO advantage and the enabler of quantified savings.  _(by 2026-07-01; owner: Deal team)_
- Re-activate champion (President) via executive connect (Carl Kimball + deal owner's manager meet with President + CFO) to address SAP cost blocker, refresh urgency on the procurement digitization business case, and secure the champion's active push to the IT steering committee for final approval within 30 days. Frame the meeting as a high-value strategic discussion on digital procurement transformation and AI-powered S2P, not a status check.  _(by 2026-07-01; owner: Executive connect)_
- Complete customer reference calls (BDO, SM, and one same-industry customer with complex SAP integration) and send curated reference list to the buyer's IT lead (Jeff De Sagun) with scheduling options within 14 days. Prioritize references that can speak to AI capabilities (BDO for Merlin AI ROI), SAP integration complexity (a customer with similar S2P-to-SAP-ERP architecture), and implementation speed (a customer who went live in under 8 months).  _(by 2026-07-08; owner: Deal team)_
- Send MSA and order form to the buyer's IT lead (Jeff De Sagun) for legal pre-review in parallel with the IT steering committee approval process, so contracting can start immediately upon committee decision. Include a cover note explaining that this is a time-saving courtesy (not presumptive) to expedite legal review, which typically takes 4-8 weeks, and avoid post-decision delays.  _(by 2026-07-08; owner: Deal team)_
- Prepare and deliver a tailored IT steering committee presentation deck (20-30 slides) for the buyer to present internally, covering functional fit validation (POC results), technical architecture alignment (S2P-to-SAP-ERP integration), AI differentiation (Merlin vs SAP Ariba), TCO advantage (Zycus + SAP integration fee vs Ariba total cost), customer proof points (BDO, SM references), and recommended next steps (MSA execution, 7-month implementation kickoff). Offer to co-present with the buyer if the committee allows vendor participation.  _(by 2026-07-22; owner: Deal team)_

**Open commitments** (1)
- Run CFO-led ROI workshop with Jonathan Back and Zeeshan Pervez to quantify savings and address SAP middleware cost in TCO terms  _(who: Zycus; open)_

### Allstate  -  Allstate - S2P 2025
_Owner: Karson Keogh  |  opp_id: 006P7000006uKrq_

**Critical moves** (5)
- Re-engage Gaurav by phone within 7 days to confirm three critical items: (a) SAP Ariba beta completion date and outcome, (b) leadership readiness for commercials and POC start, and (c) a committed date for the Casey McDowell executive session in Chicago. If no reply within 5 business days, escalate via the deal owner's manager to Casey McDowell directly with a concise executive summary: Zycus vs Ariba down-select decision, AI differentiation case, and a request for a 30-minute commercials/timeline discussion by 5 Jul.  _(by 2026-06-28; owner: Deal team)_
- Deliver the two promised AI-focused reference calls (customers actively using Merlin agentic procurement, autonomous negotiation, or AI redlining at scale) and demo recent Merlin/AI product enhancements to Casey McDowell and Cheryl Harris by 5 Jul, positioning AI as the wedge Ariba cannot match and directly addressing Allstate's AI-adoption risk concerns.  _(by 2026-07-05; owner: Deal team)_
- Multi-thread to Mohit Bothra (Product Manager, Decision Maker contact role) via a technical validation session: walk through the SAP S/4HANA + Workday integration patterns using the detailed case studies promised 21 Jan, confirm the 9-month lift-and-shift timeline and data-migration approach, and secure Mohit's written sign-off that the technical fit is validated and ready for EB commercials approval.  _(by 2026-07-05; owner: Deal team)_
- Co-author a quantified ROI business case with Allstate procurement finance: model the cost savings from consolidating Ariba P2P + Fieldglass VMS + homegrown contract/risk tools into Zycus S2P (system licensing, maintenance FTE, contract-compliance risk reduction), tie it to a specific $ or headcount target the CFO will approve, and present it to Casey McDowell and Cheryl Harris as the financial justification for the down-select by 10 Jul.  _(by 2026-07-10; owner: Deal team)_
- Lock a POC scope, start date, and success criteria with Gaurav by 12 Jul: propose a 2-week bounded POC (one use-case: AI-powered contract redlining OR Merlin autonomous intake, Allstate's choice) starting 15 Jul, with go/no-go decision by 31 Jul aligned to the close date, and position the POC as the final Ariba-vs-Zycus bake-off to force leadership decision.  _(by 2026-07-12; owner: Deal team)_

**Open commitments** (2)
- Deliver updated proposal including POC scope, commercials, and reference details (requested 28 Apr: 'fu with guarav on all pending items - POC, Reference, and updated proposal')  _(due 2026-05-15; who: Zycus; overdue)_
- Complete SAP Ariba beta/workshop and provide down-select decision or beta outcome to Zycus  _(due no due date; who: Buyer; open)_

**Implicit needs** (2)
- Confirm Ariba beta completion date and outcome, and surface a beta-exposed Ariba weakness to build competitive differentiation case  _(2026-06-16)_
- Execute the promised executive session with Casey McDowell (VP) in Chicago to lock EB sponsorship and budget authority  _(2026-05-26)_

### Amy's Kitchen Inc  -  Amy's Kitchen - P2P Nov '25
_Owner: Steve Ovadje  |  opp_id: 006P700000QHQK5_

**Open commitments** (2)
- Pre-run business case with VP of Procurement, then present to the relevant stakeholder Officer Oksana in mid-July for funding approval  _(due 2026-07-15; who: Buyer; open)_
- Complete vendor selection (evaluate Zycus vs undisclosed competitors) and choose between Scope A (P2P only) or Scope B (P2P + source-to-contract) post-funding approval  _(due 2026-07-31; who: Buyer; open)_

**Implicit needs** (1)
- Schedule follow-up meeting between Amy's AP team and Zycus to walk through invoice ingestion, two/three-way matching, and Oracle integration workflows after funding approval  _(2026-06-18)_

### ARUP Laboratories  -  ARUP_August 25'
_Owner: Steve Ovadje  |  opp_id: 006P700000LmUm5_

**Critical moves** (5)
- Build and present a quantified ROI model to Michael and Bevan within the next 7 days, before RFP proposal submission (10 Jul). Work with Michael to estimate ARUP's current manual contract cycle time (days from request to signature), error rate (missed obligations, version-control failures per year), and FTE effort (hours per contract), then model Zycus's impact using conservative assumptions (e.g. 40% cycle-time reduction, 60% error reduction, 0.5 FTE savings). Anchor the model to ARUP's articulated pain: CFO frustration over redline failures, manual Word process causing misses, no centralized risk visibility. Include this ROI model in the RFP proposal executive summary and offer to present it to Bevan and the eval committee. The goal is to give Bevan a financial case that Conga enhancement cannot match and shift him from skeptical to convinced.  _(by 2026-07-01; owner: Deal team)_
- Ask Michael for the RFP document (or at minimum the eval criteria, scoring methodology, and post-selection timeline: decision date, finalist presentations, contract-award window) within the next 3 days. Use this intel to: (a) ensure the RFP proposal heavily weights Zycus's AI risk scoring, WordConnect for external legal, clause library, and D365 integration (the gaps Conga cannot close), (b) include at least two customer references in lab/healthcare/life-sciences with external-legal workflows similar to ARUP's, and (c) pre-align on MSA/SOW terms, InfoSec requirements, and implementation scope so post-selection contract execution is fast-tracked and does not slip the 31 Jul close date. If Michael cannot share the RFP, ask for a 15-minute call to align on the above before proposal submission.  _(by 2026-06-27; owner: Deal team)_
- Secure a 30-minute pre-proposal alignment call with Michael and Ondraya (and Robert if available) within the next 10 days to: (a) review the draft RFP proposal's exec summary and ROI model (from move #1), (b) confirm the references and case studies are relevant to ARUP's workflow, and (c) identify any last-minute objections or eval-committee concerns that should be addressed in the proposal. Use this call to validate that the proposal will score well and to surface any political or technical blockers (e.g. Art's skepticism, Bevan's pricing sensitivity, legal/InfoSec requirements) that can be proactively addressed before submission.  _(by 2026-07-04; owner: Deal team)_
- Draft and send a brief (2-paragraph) email to Michael within the next 5 days summarizing the three critical differentiators Zycus offers vs Conga (AI risk scoring + metadata extraction; WordConnect for external legal collaboration; post-contract obligation dashboards + D365 integration) and asking him to confirm these are the top priorities for the eval committee. Frame this as 'ensuring our proposal addresses your top 3 must-haves' and use his reply to validate the proposal messaging. If Michael replies with different priorities, adjust the proposal accordingly. The goal is to ensure the proposal is buyer-anchored, not vendor-driven.  _(by 2026-06-29; owner: Deal team)_
- Within 14 days (before or immediately after proposal submission), ask Michael whether ARUP will accept a signed MSA by 31 Jul with Phase 1 go-live later (e.g. Oct 2026), or whether full implementation is required by 31 Jul. If the latter, flag to the Zycus delivery team that this timeline is extremely tight and may require pre-sale implementation planning (e.g. kickoff call scheduled during RFP eval, SOW pre-drafted, InfoSec questionnaire submitted before selection). If Michael confirms Phase 1 close is acceptable, this de-risks the timeline; if full implementation is required, escalate internally to ensure delivery can commit to it or re-negotiate the close date now (before RFP submission) to avoid post-selection surprise.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (1)
- Submit RFP proposal by deadline  _(due 2026-07-10; who: Zycus; open)_

### Austrian Post  -  Austrian Post 2025  - S2P and Intake
_Owner: Dirk Fischbach  |  opp_id: 006P700000J71MD_

**Critical moves** (5)
- Zycus CEO or the deal owner's manager (John Woodcock, Zycus VP) to engage Austrian Post CFO with hard business-case validation: breakeven ROI at <1% tail-spend savings (vs Pölki's 2% projection), Walmedien upgrade cost avoidance (consumes same SAP resources as new implementation, per 09 Jun call), and 2027 SAP freeze forcing decision now (defer = 2028 at earliest). Frame CIO IT objection as cost/capacity, not solution merit, and position Zycus iSaaS AI integration as the technical answer. Request CFO top-down override of CIO to secure Austria S2C commitment by end Jun to preserve 2026 go-live window.  _(by 2026-06-30; owner: Executive connect)_
- Demonstrate Zycus iSaaS AI-driven SAP integration live with Austrian Post IT team: run demo interface with customer SAP test instance and Zycus technical leads to show end-to-end mapping automation, and provide written man-days estimate from prior iSaaS S/4HANA deployments (typical effort ~30–50 days vs IT's 170-day manual estimate). Convert IT objection from abstract capacity concern to concrete, reduced-scope commitment ('we need 50 days, not 170, and can phase over Q3–Q4 2026'). Offer to schedule demo within next 7 days if IT willing to engage.  _(by 2026-07-01; owner: Deal team)_
- Propose phased Zycus platform rollout to Austrian Post: implement S2C + Intake in 2026 (lower IT load, delivers guided-buying and sourcing value immediately), defer full P2P integration to 2027 post-SAP-freeze when IT capacity reopens. Frame this as superior to competitor orchestration overlay (Tronkeon) on platform integration, reporting consistency, and AI agent benefits, while preserving Zycus P2P upsell path. Position as 'Austria S2C now, P2P when IT ready' vs 'fragmented stack with competitor bolt-on.' Offer written proposal with phased pricing and 2027 P2P commitment.  _(by 2026-07-08; owner: Deal team)_
- Complete and send all overdue buyer deliverables this week: (1) matrix representation of sourcing price sheet + S2C workflow video (requested 15 May, overdue); (2) sample CSV invoice-line-item file for reporting verification (requested 21 May, overdue); (3) written POC data-deletion confirmation with evidence (requested 09 Jun, overdue). Package and send as single coordinated delivery to Engelbert Pölki and Karin Eppich with cover note confirming all open items closed and Zycus ready to proceed once IT decision made.  _(by 2026-06-30; owner: Deal team)_
- Internally investigate and confirm within 7 days: can Zycus Intake/orchestration solution connect to non-Zycus P2P systems (e.g. competitor or legacy Walmedien) as standalone interim solution, and if so, what APIs/integration effort required? Report findings to Engelbert Pölki and Karin Eppich. Position as fallback-only option (inferior to phased Zycus platform) but available if Austrian Post IT absolutely cannot support any P2P work in 2026. Use to counter competitor Tronkeon offering by showing Zycus can also provide orchestration overlay if needed, but recommend against it.  _(by 2026-07-01; owner: Product escalation)_

**Open commitments** (7)
- Provide matrix representation of sourcing price sheet and brief video of S2C workflow to Engelbert Pölki  _(due 2026-05-22; who: Zycus; overdue)_
- Send sample CSV file with invoice line items for reporting verification  _(due 2026-05-27; who: Zycus; overdue)_
- Provide written confirmation of POC data deletion with evidence attachments  _(due 2026-06-13; who: Zycus; overdue)_
- Discuss iSaaS AI-driven SAP integration proposal with technical leadership and provide response ASAP to support accelerated timeline  _(due 2026-06-27; who: Zycus; open)_
- Investigate whether Intake solution can connect to other P2P systems and report back on feasibility  _(due 2026-06-30; who: Zycus; open)_
- Send security and data-protection documents to Zycus within next days to avoid implementation delay  _(due 2026-06-27; who: Buyer; open)_
- Verify user counts and provided list with colleagues and send validated numbers back to Dirk  _(due 2026-06-23; who: Buyer; overdue)_

**Implicit needs** (2)
- Demonstrate iSaaS AI-driven SAP integration to counter IT's 170-day analysis estimate and secure CIO buy-in  _(2026-06-24)_
- Explore phased rollout (S2C 2026, P2P deferred) or Intake standalone connectivity to competitor P2P to bypass IT bottleneck  _(2026-06-24)_

### Bang & Olufsen A/S  -  B&O_CLM_2025
_Owner: Casper Hoeholt  |  opp_id: 006P700000P69IM_

**Open commitments** (1)
- Jonas (or B&O team) to confirm next meeting date post-'later June' and re-engage on commercials, sandbox, and board timeline  _(due 2026-06-30; who: Buyer; open)_

### Bass Pro, LLC  -  BassPro_March'26
_Owner: Mario Castro  |  opp_id: 006P700000VCKnV_

**Critical moves** (5)
- Deliver the 2-3 customer reference calls David requested (prior Coupa P2P replacements, change-management focus) and use them as the re-engagement hook: call David and Whitney today (24 June) with a specific offer ('I have three customers lined up who replaced Coupa P2P with Zycus and are willing to speak with you this week about their transition experience and adoption results; can we schedule 30 minutes tomorrow or Friday to brief you, then set up the reference calls next week?'). This is the highest-leverage move because it addresses David's explicit decision gate and creates a concrete near-term next step.  _(by 2026-06-25; owner: Deal team)_
- Request David facilitate a 45-minute AP alignment workshop within the next 10 days, positioned as a collaborative session where Zycus walks the AP team through invoice workflow, 2-way/3-way matching configuration, and ERP integration to address their stated concerns about transition risk. Offer to bring a Zycus AP-focused solution consultant and a customer AP lead (from a reference account) to co-present. Frame it as removing David's internal blocker rather than a sales pitch.  _(by 2026-07-05; owner: Deal team)_
- Co-author a mutual close plan with David that walks backward from 30 July close date, naming the gates (commercial sign-off, legal/InfoSec review, SOW/MSA negotiation, signature), the owner for each gate (David, AP, CFO, legal, Zycus), and the required completion date for each milestone. Deliver this as a one-page visual timeline in the next conversation (following the reference-call re-engagement) and ask David to confirm or revise it. If he cannot commit to the plan, the close date slips and must be updated in Salesforce.  _(by 2026-07-01; owner: Deal team)_
- If no buyer reply by 26 June (2 business days post-reference offer, 11 days total silence), execute the thoughtful escalation outlined in the Next Step: send a brief, value-focused email to the CPO (David's boss, unmapped) and CFO (unmapped), cc'ing David, summarizing the evaluation to date, noting David's request for Coupa-replacement references which are now available, and requesting a 30-minute executive alignment call to confirm strategic fit and timeline before the 30 July decision window closes. Position it as partnership-driven (helping them hit their evaluation deadline) rather than pressure.  _(by 2026-06-27; owner: Executive connect)_
- Send the Horizon 2025 event recordings link and a curated 10-minute highlight reel focused on customer change-management case studies and Coupa-to-Zycus migrations, with a note: 'David, while we finalize the live reference calls, here's a preview of how other customers approached the P2P transition—thought this might be useful context for your AP and leadership conversations.' Deliver within 48 hours.  _(by 2026-06-26; owner: Deal team)_

**Open commitments** (4)
- Deliver 2-3 customer references from prior Coupa P2P replacements, specifically addressing transition experience, user adoption, and change-management approach  _(due 2026-06-10; who: Zycus; overdue)_
- Send link to prior year's Horizon event recordings  _(due 2026-06-03; who: Zycus; overdue)_
- Schedule deep-dive session on iSource, iContract, iSupplier modules after 2-week internal socialization period  _(due 2026-04-20; who: Buyer; overdue)_
- Review updated proposal and confirm scope/pricing alignment  _(due 2026-06-24; who: Buyer; overdue)_

**Implicit needs** (2)
- Deliver structured change-management plan (workshops, lunch-and-learns, on-site open office sessions) as contractual deliverable to de-risk 2,000-user adoption  _(2026-04-06)_
- Conduct alignment session with AP team to address their concerns about transition and build internal sponsorship  _(2026-04-06)_

### BitSight  -  BitSight P2P Evaluation
_Owner: Justin Ajmo  |  opp_id: 006P700000Rwszl_

**Critical moves** (5)
- Re-engage Amy directly with a forcing question and a date: send a concise email summarizing the 2026-01-16 demo fit (reliability, dashboards, auto-PO, Merlin), naming the GEP pain she articulated, and asking explicitly: 'Are we still in your evaluation, and if so, what is the single next step and date to move forward (broader-team call, down-select outcome, or commercials)? If not, what changed?' Request a 15-minute call by 2026-06-27 to decide together whether to advance or part ways. This is a qualify-or-disqualify move, not a chase.  _(by 2026-06-27; owner: Deal team)_
- Map and engage the Economic Buyer (CFO or VP Operations) and the GRC lead in parallel via Amy's introduction or LinkedIn/direct outreach if she is unresponsive. Frame the outreach as a quick executive check-in on procurement-platform strategy and GEP pain (not a demo pitch): 'Amy shared the chronic GEP stability issues your procurement team faces — as you evaluate a platform switch, I'd like to understand your priorities (reliability SLA, implementation risk, budget/ROI) and share how we've solved this for. 20 minutes this week or next?' Book both conversations by 2026-07-05.  _(by 2026-07-05; owner: Deal team)_
- Deliver a reliability proof package by 2026-07-08: (a) three reference customers (similar-size P2P implementations) willing to speak on Zycus platform stability and uptime vs prior systems, (b) a written uptime SLA commitment (99.X% with support-response tiers), and (c) a one-page GEP-vs-Zycus stability comparison (Zycus enterprise architecture, redundancy, change-management discipline vs GEP's chronic-bug profile Amy described). Offer to run a joint GEP-Zycus bake-off workshop with BitSight IT/GRC where both vendors present their stability/support model and Amy's team scores them. Send the package to Amy and offer the bake-off as the next milestone if she re-engages per rank-1 move.  _(by 2026-07-08; owner: Deal team)_
- If Amy re-engages (rank-1 move succeeds) and the EB/GRC are mapped (rank-2), co-author a mutual close plan with Amy anchored to a realistic close date (likely 2026-08-30 to 2026-09-15, not 2026-07-29): down-select/finalist confirmation by, broader-team eval (GRC + EB) by, commercials/pricing by, reliability bake-off or reference calls by, NetSuite integration scoping by, contracting (MSA/SOW) by, final approvals by, signature by. Document it in a one-page joint timeline, get Amy and the EB to agree, and treat each milestone as a mutual commitment. Update the relevant stakeholder to the mutually agreed date.  _(by 2026-07-08; owner: Deal team)_
- If no substantive buyer engagement (Amy reply, EB/GRC meeting, or down-select outcome) occurs by 2026-07-10 (16 days, following ranks 1-3), escalate to executive connect: the deal owner's manager reaches the BitSight exec sponsor (CFO or VP Ops, to be identified via LinkedIn/Amy if rank-2 succeeds) for a peer-to-peer conversation framing the situation honestly: 'Your procurement team engaged us in January on the GEP stability pain and we demonstrated strong fit, but the evaluation has gone quiet for 5 months and we're unclear if BitSight is still prioritizing a platform switch or if something changed (budget, timeline, GEP counter-offer). Before we close this out on our side, I wanted to check directly with you: is a procurement-platform change still a priority for BitSight this year, and if so, what would it take to make a decision in the next 30-45 days?' Request a 20-minute call by 2026-07-15.  _(by 2026-07-15; owner: Executive connect)_

**Open commitments** (1)
- Position and deliver a call to discuss GEP differentiation on service/support (rep-initiated, awaiting buyer acceptance)  _(who: Zycus; open)_

### Bright Horizons Family Solutions  -  Bright Horizons 2025 - S2P
_Owner: Claire Hudson  |  opp_id: 006P700000JwvB3_

**Open commitments** (2)
- Andrew Duckworth to provide feedback on final commercial pack  _(due 2026-06-21; who: Buyer; overdue)_
- Organize integration call to discuss Workday ERP endpoints and payment posting  _(who: Zycus; open)_

**Implicit needs** (1)
- Confirm IT/integration workstream resourcing and InfoSec review completion before contract signature  _(2026-06-09)_

### Cadence Design Systems, Inc.  -  CadenceDesign_CLM'26
_Owner: Karson Keogh  |  opp_id: 006P700000ShX3q_

**Critical moves** (5)
- Map the Economic Buyer (CFO or finance VP) and deliver a quantified ROI case in an executive-to-executive session by 5 Jul to de-risk the mid-July decision, validate the business case resonates with the finance lens, and pre-empt an Ironclad price undercut or budget-cut / defer decision.  _(by 2026-07-05; owner: Executive connect)_
- Validate Peter Hughes as champion (or recruit a replacement advocate if Peter is neutral) and weaponize the competitive wedge (end-to-end platform + SAP contract enforcement + Merlin agentic AI intake vs. Ironclad legal-ops-only positioning) in a champion-enablement session by 28 Jun, so the internal advocate can carry the wedge into the mid-July EB room and block Ironclad's narrative.  _(by 2026-06-28; owner: Deal team)_
- Create decision urgency by identifying a business forcing function (H2 2026 fiscal planning / efficiency-gain capture, M&A due-diligence pipeline, or new-product contract launch) and quantifying cost-of-delay, then incorporate the forcing function into the 5 Jul EB ROI session to re-anchor the mid-July timeline and reduce slip risk.  _(by 2026-07-01; owner: Deal team)_
- Request a legal + procurement checkpoint call with Melanie Hamm, Dave Bebb or Issac Lin (legal counsel), and Peter Hughes by 3 Jul to confirm (a) MSA redline status and any open legal points, (b) SOW draft status, (c) InfoSec sign-off completion, and (d) the post-EB-approval contracting timeline (days from EB yes to signature), so Zycus can sequence the final push and avoid a post-decision contracting delay.  _(by 2026-07-03; owner: Deal team)_
- Obtain product-team resolution on the complex coded-template logic requirement (conditional clause removal + multi-selection driven by Salesforce metadata for SDK + config-tool scenarios) and communicate outcome (feasibility + timeline, or finalized backup plan: Salesforce initiation then Zycus authoring) to Melanie Hamm by 2 Jul, so this open technical requirement does not become a last-minute objection in the mid-July EB decision or lower legal's confidence vs. Ironclad.  _(by 2026-07-02; owner: Deal team)_

**Implicit needs** (2)
- Legal and IT teams require detailed architecture, security, and integration documentation (multi-tenant isolation, SOC1/2, BCP, data-backup strategy, Salesforce + iManage + SAP integration guides) to complete InfoSec and technical-diligence review ahead of EB decision  _(2026-04-06)_
- Provide Zycus master services agreement (MSA) immediately to enable parallel legal review by Cadence procurement and legal teams, avoiding contract-review bottleneck post-EB decision  _(2026-04-21)_

### Cebu Pacific Air  -  Cebu Pacific
_Owner: Carl Kimball  |  opp_id: 0066700000wdNe1_

**Critical moves** (5)
- Re-engage the champion (Astrid Collado-Tanquieng) within 48 hours to confirm her continued sponsorship, understand internal status post-22 Jun outreach, and secure her advocacy to broker a tri-party executive session (CFO, CPO, CIO) before ProcureCon. Frame the call as a ProcureCon/Horizon prep session and ask Astrid to confirm the CPO's and CFO's attendance and availability for a face-to-face commercial alignment meeting 8-9 Jul.  _(by 2026-06-26; owner: Deal team)_
- Deliver ST Engineering contact and offer joint reference call or Singapore site visit to the CFO (Mark Cezar), CPO (Anna Pamela Mollasgo-Dela Pasion), and champion (Astrid) by 1 Jul, positioning it as a pre-ProcureCon peer proof point on parallel SAP S/4HANA + S2P implementation. Frame the reference as addressing the buyer's explicit 19 May request for lessons learned and de-risking the SAP migration dependency that is blocking their decision.  _(by 2026-07-01; owner: Deal team)_
- Executive connect (the deal owner's manager) to broker a tri-party face-to-face session at ProcureCon Singapore (8-9 Jul) with the CFO (Mark Cezar), CPO (Anna Pamela Mollasgo-Dela Pasion), and CIO (Laureen Cansana) to align on phased AP-first roadmap (Phase 1 non-integrated or light-API AP automation for immediate ROI, Phase 2 full S2P post-SAP S/4HANA cutover), flexible year-1 commercial terms (low upfront, deferred payment), and SAP migration de-risking via the relevant stakeholder proof point. Position Zycus as the vendor who solves the SAP dependency problem, not the vendor waiting for it to resolve.  _(by 2026-07-08; owner: Executive connect)_
- Co-build a quantified ROI model with Astrid (champion), Hazel Navarro (AP Manager), and the CFO's finance team tying AP automation to measurable cost/time savings (touchless invoice processing rate, manual AP headcount reduction, reimbursement cycle-time improvement) and working capital optimization (days payable outstanding improvement, cash flow extension). Frame it as a Phase 1 quick-win business case that funds itself and extends their runway while SAP S/4HANA completes, addressing the 19 May funding caution and flexible-terms request.  _(by 2026-07-08; owner: Deal team)_
- Position Merlin agentic AI (negotiation agent for supplier price history, contract intelligence) and I2O (procurement orchestration) as the Phase 2 competitive wedge in the tri-party executive session and ProcureCon meetings, framing it as the future-proof S2P roadmap that aligns to the relevant stakeholder stated AI curiosity and counters Coupa's AI positioning. Tie Merlin to airline-specific use cases (fuel, MRO, catering procurement where price negotiation and contract intelligence deliver outsized ROI) and make it the centerpiece of the S2P extensibility narrative.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (3)
- Provide ST Engineering contact for reference call on parallel SAP S/4HANA + S2P implementation  _(due 2026-07-01; who: Zycus; overdue)_
- Confirm attendance and meeting schedule at ProcureCon Singapore (8-9 Jul 2026) and Horizon SEA  _(due 2026-07-08; who: Buyer; open)_
- Respond to rep outreach re ProcureCon and Horizon discussion with CPO  _(due 2026-07-01; who: Buyer; overdue)_

**Implicit needs** (1)
- Co-build a quantified ROI model tying AP automation to measurable cost/time savings and working capital optimization  _(2026-05-19)_

### Chantier Davie Canada  -  Chantier Davie Canada OppS2C - Jan'25
_Owner: Mike Flowers  |  opp_id: 006P700000GnpY2_

**Critical moves** (5)
- Executive connect: the deal owner's manager to initiate peer-level conversation with the VP of the relevant stakeholder (Economic Buyer) to negotiate a committed 30-day post-Oracle-Phase-3 eval window, secure a binding go/no-go decision date (target end of Q3 2026), and either restart the RFP or disqualify cleanly. Frame as: 'Zycus automates the procurement workflows Oracle leaves manual and accelerates your Oracle ROI by reducing manual RFI/RFP/contract load on your team NOW, while Phase 3 is still underway—let's ring-fence 30 days in Q3 to complete shortlist and commercials or formally close the eval.' Position Zycus as Oracle's procurement accelerator, not its bandwidth competitor.  _(by 2026-07-01; owner: Executive connect)_
- Deal team: re-activate the champion (Project Procurement Manager) directly via personal outreach (phone + email) to assess whether he retains internal advocacy post-Oracle Phase 3 override, arm him with a procurement-ROI + Oracle-integration narrative that separates Zycus value from Oracle resource load, and enlist him to drive an internal late-Q3 2026 forcing event (exec review, formal go/no-go gate) to break the RFP-collapse stall. If he confirms he is politically sidelined or no longer engaged, escalate to rank-1 exec connect immediately.  _(by 2026-07-01; owner: Deal team)_
- Deal team: deliver a 30-minute executive briefing (virtual) to the VP of the relevant stakeholder (Economic Buyer) + the VP of Information Technology (Oracle integration gatekeeper) presenting the joint Oracle-integration + procurement-ROI narrative: 'Zycus accelerates Oracle Fusion payback by automating RFI/RFP/supplier onboarding/contract authoring workflows that Oracle leaves manual, reducing your team's manual load NOW (during Phase 3) rather than waiting for Oracle to stabilize. Our S2C layer integrates WITH Oracle and offloads procurement tasks so your Phase 3 resources can stay focused on ERP without sacrificing procurement efficiency.' Objective: convert the Oracle blocker into a Zycus business case and secure agreement to a Q3 2026 eval restart window.  _(by 2026-07-08; owner: Deal team)_
- Deal team: deliver written security attestation or data-handling documentation addressing government data confidentiality requirements (requested implicitly by Economic Buyer 11 Apr 2025: 'if we can demonstrate that we have security in place to not be sharing specifically data from the government') and formal Canadian data residency confirmation or hosting-option statement (requested by the relevant stakeholder 13 Feb 2025: 'where is our data stored?'). Package both as a single 2-page 'Government Data Security & Residency Briefing' and send to Economic Buyer + Legal Counsel to remove the two unresolved compliance gates blocking AI adoption and contract approval.  _(by 2026-07-08; owner: Deal team)_
- Deal team: if no buyer reply to rank-1 exec connect or rank-2 champion outreach within 10 business days (by ~15 Jul 2026), formally disqualify the opportunity and mark stage as Closed Lost - No Decision, recording Oracle Phase 3 as the loss reason. Document the qualified pain, confirmed fit, and buyer-stated RFP-collapse intent as the disqualify rationale, and propose a re-engagement trigger: 'Zycus will check back in Q1 2027 (post-Oracle Phase 3 stabilization per original timeline) to assess whether procurement automation has re-emerged as a priority.' Do not leave the deal in Formal Evaluation / Best Case indefinitely with no buyer engagement.  _(by 2026-07-15; owner: Deal team)_

### Checkers - Rally's  -  Checkers - Rally's_ Opp - February '26
_Owner: Grace Kim  |  opp_id: 006P700000Tbxqb_

**Critical moves** (5)
- Re-engage the buyer immediately to restart the relationship. Schedule a discovery call with Leslie, Josh Ullrich, and the third unnamed contact to surface the S2P RFP release timeline (promised first week July), validate Zycus is still in scope, quantify the business problem and ROI, identify the Economic Buyer and Decision Maker, and map the evaluation and approval process. Position Merlin AI differentiation (negotiation agent, intelligent intake) as a wedge.  _(by 2026-06-28; owner: Deal team)_
- Obtain the full S2P RFP requirements immediately upon release (promised first week of July) and conduct a fit assessment against the scope-control RFP fitment gaps that caused Zycus withdrawal 12 Mar. Map Zycus capabilities (iSupplier, Merlin Intake, integration breadth) against buyer criteria before submission. If fit gaps remain, surface them to the deal owner's manager and product escalation to determine go/no-go before investing in RFP response.  _(by 2026-07-07; owner: Deal team)_
- Identify and activate the Economic Buyer. Discovery the buyer org chart (CFO, Chief Supply Chain Officer, procurement VP) and map who controls the $125K budget and drives the S2P initiative internally. Schedule an executive connect with the deal owner's manager to engage the EB directly, quantify the business case, and secure active sponsorship. Without EB alignment, commercials and contracting cannot proceed.  _(by 2026-07-15; owner: Executive connect)_
- Develop a champion. Identify the buyer stakeholder with the strongest pain (procurement inefficiency, supplier risk, compliance burden) and EB/DM access, and equip them to advocate internally for Zycus. Share competitive positioning (Merlin AI vs Coupa/Ariba), integration proof points, and ROI case studies. Make them the internal navigator for the S2P RFP evaluation and selection process.  _(by 2026-07-15; owner: Deal team)_
- Reconcile the close date against the S2P RFP timeline. If RFP releases first week of July, a typical enterprise S2P evaluation and contracting cycle is 4-6 months (Oct-Dec close). The current close date of 27 Aug is structurally impossible. Work with the buyer to establish a realistic timeline and update the close date in Salesforce to match, or push for an expedited sole-source process if Zycus is the only vendor in scope (validate with buyer).  _(by 2026-07-15; owner: Deal team)_

**Open commitments** (3)
- Release S2P RFP (beginning of Q3 2026 per Leslie 4 Jun)  _(due 2026-07-01; who: Buyer; overdue)_
- Release S2P RFP (beginning of Q3 2026 per Leslie 4 Jun response, now first week July per rep note 22 Jun)  _(due 2026-07-07; who: Buyer; overdue)_
- Release S2P RFP (first week of July 2026)  _(due 2026-07-07; who: Buyer; open)_

### CHEP  -  CHEP Opp - November '25
_Owner: Marc Quessenberry  |  opp_id: 006P700000Q0UvB_

**Critical moves** (5)
- Re-engage Kelley Benson with a low-ask value-add (one-page Merlin/ANA ROI benchmark from a peer enterprise: similar industry, tail-spend profile, quantified FTE + savings results) and ask directly: 'Who approves the $300k investment on your side—your CFO, SVP Supply Chain, or do you control the budget? Can we get 20 minutes with that person to align on the business case before we reschedule the discovery/demo?' If no reply in 7 business days, escalate.  _(by 2026-07-01; owner: Deal team)_
- If Kelley does not respond to the rank-1 re-engagement attempt within 7 business days, escalate via executive connect: the deal owner's manager reaches out to CHEP's SVP Supply Chain or CFO (leveraging the existing Zycus CLM customer relationship as the warm intro) for a 15-minute exploratory call: 'We've been working with Kelley on an AI-driven S2P expansion—Merlin Intake and autonomous tail-spend negotiation. Where does this sit in your priorities, and is there a business case / budget conversation we should be part of?'  _(by 2026-07-15; owner: Executive connect)_
- Propose a 90-minute joint ROI workshop (Kelley + her AP/procurement leads + Zycus) where Zycus runs a real CHEP tail-spend use case (100 recent office-supply or MRO PRs) through a Coupa-vs-Merlin side-by-side: (A) current Coupa form-based flow (time, touches, zero negotiation), (B) Merlin conversational intake + Anna auto-negotiation. Quantify the delta (FTE hours saved, cycle time, 3% tail-spend savings) and deliver a one-page business case: '$X annual savings, Y FTE hours redeployed, 12-month payback on $300k'. Frame it as 'Let's co-build the ROI model you can present to your CFO to unlock the budget.'  _(by 2026-07-15; owner: Deal team)_
- Secure Imon Ahmed (Director of Global P2P, the likely tail-spend process owner) and an IT/systems lead for the rescheduled discovery call by asking Kelley: 'For the discovery to be productive, we need the folks who live the tail-spend pain daily and the IT lead who will own the Merlin integration—can you pull in Imon Ahmed and your systems lead?' If she cannot or will not, treat that as a hard signal the deal lacks internal sponsorship and recommend moving to Pipeline or disqualifying until a real sponsor emerges.  _(by 2026-07-15; owner: Deal team)_
- If the above re-engagement and escalation attempts yield no buyer response or commitment by 15 Jul (21 days from today), recommend moving the deal to Pipeline (or disqualifying) and recording the honest read: 'CHEP is an existing CLM customer; Kelley (DM, Head of Indirect S2P) showed initial AI interest but the evaluation stalled post-intro—no EB mapped, no budget confirmed, Coupa incumbent satisfactory, 3-month silence post-cancellation. Will revisit if/when CHEP surfaces a funded S2P project with exec sponsorship.' Stop spending cycles on a ghost.  _(by 2026-07-22; owner: Deal team)_

### CLARINS  -  Clarins_S2P
_Owner: Pierre Meraud  |  opp_id: 006P700000W1Y21_

**Open commitments** (5)
- Second demonstration session centered on Clarins use cases (marketing procurement, catalog, services)  _(who: Zycus; open)_
- Validation of ideal project timeline (Q2 validation, Q3/Q4 RFP, Q1 2027 selection)  _(who: Buyer; open)_
- Pierre to send email and schedule call with Alice and Marilyn to prepare business case for CPO  _(who: Zycus; open)_
- Pierre to send email and schedule call with Alice Levan and Marilyn Bordais to prepare business case for the CPO  _(who: Zycus; open)_
- Business case co-creation with Alice and Marilyn to convince family ownership  _(due 2026-07-15; who: Zycus; open)_

### Conga  -  Conga_S2P_2026
_Owner: Bailey Erazo  |  opp_id: 006P700000QyOq5_

**Critical moves** (5)
- Direct re-engagement with Andy Simmons to understand the 4-week silence, validate internal timeline post-PROS acquisition, and propose a mutual close plan with three named milestones: (1) Economic Buyer briefing or ROI workshop with finance/IT exec by 2026-07-15, (2) finalist presentation or competitive bake-off by 2026-08-05, (3) contract/SOW kickoff by 2026-08-26. Frame as partnership to hit the September close: 'Andy, we delivered a strong demo in May and offered NetSuite integration in June—what has shifted internally, and how do we co-build the path to a September decision?'  _(by 2026-06-30; owner: Deal team)_
- Executive connect: the deal owner's manager engages Conga's finance or procurement leadership (Ian Wathen VP Corporate Finance, or procurement VP if identified) to validate the opportunity, map the Economic Buyer and decision process, and confirm Zycus's standing in the RFP shortlist. Position as exec-to-exec strategic discussion: procurement transformation via AI-driven sourcing, S2P ROI for a high-growth SaaS business, and partnership to de-risk implementation post-IT restructuring. Secure EB briefing or ROI workshop as the outcome.  _(by 2026-07-08; owner: Executive connect)_
- Discover the competitive shortlist and build an explicit wedge strategy. In the next conversation with Andy or an exec, ask directly: (1) who else is in the final evaluation, (2) how Zycus is currently ranked, (3) what decision criteria weighting favors (price vs fit vs AI vs speed), and (4) where Zycus is exposed. Once the shortlist is known, activate differentiation: Merlin AI negotiation agents as the sourcing-transformation wedge, native-built S2P suite (vs bolt-on competitors), deep NetSuite API coverage, Zycus-led implementation (no partner handoff), and Teams-first UX. Prepare a one-page competitive battle card for the finalist presentation.  _(by 2026-07-08; owner: Deal team)_
- Build and deliver a quantified business case (ROI one-pager) for an Economic Buyer or CFO audience, translating the operational pain (email inefficiency, no sourcing practice, manual AP) into financial impact: projected savings from sourcing enablement (% spend under management increase, negotiation yield lift via Merlin), AP cycle-time reduction and error-rate improvement (hours saved, FTE redeployment), and intake automation efficiency (reduction in procurement inquiry volume). Anchor to Conga's scale (employee base post-PROS acquisition, procurement spend baseline) and position the S2P investment as a strategic capability build, not a tactical tool purchase. Use this as the artifact for the EB briefing (rank-2 move).  _(by 2026-07-08; owner: Deal team)_
- Deliver the post-demo commitments made on 2026-05-06: preliminary quote template, integration slide deck (NetSuite PR/PO/AP flow, Conga CLM metadata sync, Teams UX), workflow-builder screenshots, Zylow benchmarking brief, spend-analytics demo invite, and concise AP payment-process overview per the AP lead's request. Package as a 'Conga S2P Business Case & Integration Runbook' and send to Andy with a specific ask: 'Please share with your finance and IT leadership and let's schedule the EB briefing and technical integration call by.'  _(by 2026-06-28; owner: Deal team)_

**Open commitments** (1)
- Andy Simmons to reply to rep's NetSuite integration offer (2026-06-17) and confirm next steps  _(who: Buyer; open)_

**Implicit needs** (1)
- Deliver concise payment-process overview and AP Smartdesk automation details to address AP lead's (Ida Stark) manual MineralTree drag-and-drop pain and credit-memo workflow questions  _(2026-05-06)_

### Consumer Cellular, Inc  -  Consumer Cellular__2025
_Owner: Karson Keogh  |  opp_id: 006P700000OcxpH_

**Critical moves** (5)
- Executive connect: the deal owner's manager to engage Jason Chan (Controller) or CFO directly to secure budget approval, confirm pricing acceptance, and map post-legal approval timeline before June 30  _(by 2026-06-26; owner: Executive connect)_
- Deal team to confirm Garry Hill as champion replacement: schedule 1:1 to assess advocacy strength, EB access, and willingness to drive internal approval in Abe's absence  _(by 2026-06-26; owner: Deal team)_
- Deal team to deliver NetSuite live integration and Search Genie recorded videos (committed June 28) and schedule follow-up with Legal/Garry to confirm MSA/SOW approval status and contracting next steps  _(by 2026-06-27; owner: Deal team)_
- Deal team to position Merlin negotiation intelligence directly to Abe (when available) or Garry/Jason Chan, tied to NetSuite integration demo: frame as Phase 1 AI-powered supplier price negotiation capability that Ariba/Coupa lack  _(by 2026-06-30; owner: Deal team)_
- Deal team to request MSA/SOW redline status and legal approval timeline from Bobbie Reyes (General Counsel) or Legal counterpart, confirming contracting authority and next steps post-CLM session  _(by 2026-06-27; owner: Deal team)_

**Open commitments** (18)
- Abe Comarow to provide feedback on how the CFO readout went per Next_Step__c 5.12.26  _(who: Consumer Cellular; open)_
- Scheduling response from Consumer Cellular for AP demo, Legal/CLM chat, and IT sessions per Next_Step__c 5.27.26  _(who: Consumer Cellular; overdue)_
- Schedule and deliver live NetSuite integration demonstration  _(due 2026-06-18; who: Zycus; overdue)_
- Scheduling response from Consumer Cellular for IT sessions  _(who: Buyer; overdue)_
- Live NetSuite integration demonstration  _(who: Zycus; open)_
- Feedback on CFO business case readout (May 8)  _(who: Buyer; overdue)_
- Deliver live NetSuite integration demonstration  _(who: Zycus; open)_
- Provide Search Genie recorded video  _(who: Zycus; open)_
- Deliver live NetSuite integration demonstration and Search Genie AI recorded videos  _(due 2026-06-24; who: Zycus; overdue)_
- Follow up with Joseph Reiber regarding contract term modification in agreements and provide answer  _(due 2026-06-24; who: Zycus; open)_
- NetSuite live integration demonstration video (recorded demo showing real-time PO, GR, invoice sync and data flow)  _(due 2026-06-27; who: Zycus; overdue)_
- Search genie recorded video demonstration (clause/keyword search across migrated contracts)  _(due 2026-06-27; who: Zycus; overdue)_
- Certinal e-signature pricing (cost per envelope, internal vs. external signature options)  _(due 2026-06-27; who: Zycus; overdue)_
- Abe Comarow to provide internal/external envelope count for Certinal pricing  _(who: Buyer; open)_
- NetSuite live integration recorded videos  _(due 2026-06-30; who: Zycus; open)_
- Search Genie recorded videos  _(due 2026-06-30; who: Zycus; open)_
- Abe to provide internal/external envelope count for Certinal pricing  _(due 2026-06-30; who: Buyer; open)_
- Deliver NetSuite live integration and Search Genie recorded videos  _(due 2026-06-30; who: Zycus; open)_

### Cornell University  -  Cornell_Opp_Feb'26
_Owner: Justin Ajmo  |  opp_id: 006P700000TYaun_

**Open commitments** (1)
- Reply to 23 Jun Horizon invite (CPO Henderson + director of contracts)  _(who: Buyer; open)_

### DuBois Chemicals Inc.  -  DuBois Chemicals - December '25
_Owner: Grace Kim  |  opp_id: 006P700000QcBt7_

**Critical moves** (5)
- Executive connect: the deal owner's manager (Michael McCarthy) to call Jackie Zhang (VP Procurement, Decision Maker) this week to re-establish executive alignment, surface any internal blockers causing the June 1 regrouping, confirm July reconnect timing, and position upcoming recap demo as final validation gate before contract. Goal: restart champion engagement and create urgency anchor.  _(by 2026-06-27; owner: Executive connect)_
- Deal team to map and engage Economic Buyer (CFO or VP Finance) before recap demo. Secure 15-minute intro call to present ROI framework (indirect spend consolidation, Merlin AI negotiation savings, compliance risk reduction) and confirm budget authority for $115k decision. Position EB as sponsor for July contract close.  _(by 2026-07-05; owner: Deal team)_
- Deal team to prepare and deliver recap demo for new stakeholders early July (per buyer June 1 commitment 'reconnect end of Q2 to schedule recap demo'). Demo agenda: (1) Merlin AI negotiation agent and intake automation as differentiation vs. Ariba/Coupa, (2) unified indirect suite (CLM, iSource, Spend, analytics dashboards) addressing DuBois's siloed-tools pain, (3) ROI case study from chemicals/manufacturing customer. Invite Jackie, Tharun, Chris (Legal), Ryan (ERP), new stakeholders, and push for EB attendance.  _(by 2026-07-10; owner: Deal team)_
- Deal team to run competitive review with Jackie and Tharun before or during recap demo. Surface SAP Ariba's enterprise complexity, integration cost, and lack of purpose-built indirect AI vs. Zycus Merlin. Present Coupa's narrow sourcing-only scope vs. Zycus unified suite. Deliver one-page competitive wedge document positioning Merlin negotiation agent + CLM + analytics as the differentiation Ariba/Coupa lack.  _(by 2026-07-10; owner: Deal team)_
- Deal team to develop quantified ROI case with Tharun and Jackie: indirect spend consolidation savings (% of addressable spend), Merlin AI negotiation savings estimate (supplier price optimization), CLM cycle-time reduction (contract turnaround days), and compliance risk mitigation (audit readiness). Deliver one-page ROI summary before recap demo to arm Jackie for internal budget approval and EB sign-off.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (5)
- Follow up on MSA redlines and InfoSec requirements  _(due 2026-06-22; who: Zycus; open)_
- DuBois to reconnect end of Q2 to schedule recap demo for all new stakeholders (stated June 1). End of Q2 is June 30; today is June 16. Buyer has not reconnected.  _(due 2026-06-30; who: Buyer; open)_
- Reconnect end of Q2 (by June 30) to schedule recap demo for all new stakeholders being looped in  _(due 2026-06-30; who: Buyer; open)_
- DuBois to reconnect end of Q2 (by June 30) to schedule recap demo for all new stakeholders being looped in  _(due 2026-06-30; who: Buyer; overdue)_
- Provide updated scope confirmation and pricing tailored to shifted priorities when buyer reconnects (buyer stated June 1 internal guidelines being created; rep should be ready to address in early July reconnect)  _(due 2026-07-07; who: Zycus; open)_

### Farm Credit Canada  -  FCC_S2C_RFP_Jan'26
_Owner: Bailey Erazo  |  opp_id: 006P700000S00xa_

**Open commitments** (5)
- Provide FCC with default template matrices and storage-capacity details  _(who: Zycus; overdue)_
- Provide detailed answers on RFP locking restrictions, vendor portal closing timing, cycle time notification settings, and initial storage capacity  _(who: Zycus; overdue)_
- Share visuals of best-practice configuration templates for a sourcing solution  _(who: Zycus; overdue)_
- Check with product team on how to enforce event-level submission locking after RFP close  _(who: Zycus; overdue)_
- FCC to finalize its S2P platform decision by middle of next week (~22 Apr)  _(due 2026-04-22; who: Buyer; overdue)_

**Implicit needs** (1)
- Check with product team on how to enforce event-level submission locking after RFP close  _(2026-04-14)_

### Fortive Corporation  -  FortiveIntake Merlin_May'26
_Owner: Rick Taranek  |  opp_id: 006P700000XgoYU_

**Critical moves** (5)
- Schedule a joint discovery call with Alex Becker and the strategic procurement director (Director of Indirect Procurement Global) to validate timeline, map budget authority and approval chain, and align Intake rollout with Phase 1 S2P go-live completion and organizational AI-readiness gates; position this as pre-briefing for Horizon attendance decision  _(by 2026-07-01; owner: Deal team)_
- Deliver the three open commitments from the 12 Jun demo (L2 punchout feasibility confirmation, Horizon event details with the relevant stakeholder logistics, demo recording + Showpad folder links) to Alex Becker in a single follow-up package, and include a lightweight Intake adoption-impact brief (peer use-case, user-NPS lift, rogue-buy reduction %) to support his internal socialization with the strategic director  _(by 2026-06-28; owner: Deal team)_
- Correct the opportunity close date to Q1 2027 (earliest realistic timeline given buyer's stated late-2027/2028 Intake rollout window and Phase 1 S2P dependency) and recommend Pipeline forecast to the deal owner's manager to align pipeline integrity with buyer readiness and remove forecast-credibility risk  _(by 2026-06-30; owner: Deal team)_
- Co-develop a lightweight change-management and adoption-readiness brief with Alex Becker addressing legacy power-user resistance, IT/InfoSec approval process for Merlin/AI tooling, and phased rollout plan (pilot → BU expansion) aligned with Fortive's multi-ERP environment; use peer case studies from similar complex enterprises  _(by 2026-07-08; owner: Deal team)_
- Secure Fortive attendance commitment at Horizon (21-23 Sep, Beaver Creek CO) by mid-July, pre-brief Alex and the strategic procurement director on session selection (AI Console roadmap, peer Intake adoption panels, executive networking with CPOs/VPs from similar multi-ERP enterprises), and schedule a post-Horizon debrief (week of 28 Sep) to convert event insights into a joint action plan (pilot scoping, business-case co-development, or Phase 2 SOW kickoff)  _(by 2026-07-15; owner: Deal team)_

**Open commitments** (4)
- Confirm feasibility of surfacing L2 external-catalog search results (Amazon Business, CDW, Dell punchouts) within Merlin chat interface and report back to Alex Becker  _(who: Zycus; open)_
- Send Alex Becker Horizon event details (dates: 21-23 Sep, location: Beaver Creek CO, agenda) and logistics to enable Fortive team attendance evaluation  _(who: Zycus; open)_
- Email Alex Becker the 12 Jun demo meeting recording and related Showpad folder links for internal socialization  _(who: Zycus; open)_
- Establish regular cadence (quarterly or semi-annual) of roadmap and AI Console updates for Alex Becker and Fortive procurement team  _(who: Zycus; open)_

**Implicit needs** (2)
- Share meeting recording and Showpad demo materials with Alex Becker to support internal socialization and reporting to the strategic procurement director  _(2026-06-12)_
- Provide change-management best practices and user-adoption playbook to address legacy power-user resistance and cultural barriers to chat-based requisitioning  _(2026-06-12)_

### Global Switch  -  Globalswitch_S2P_2026
_Owner: Claire Hudson  |  opp_id: 006P700000VSLhB_

**Open commitments** (3)
- Deliver final use-case validation demo covering (1) user-guided requisition journey, (2) contract lifecycle/redlining for legal (Word add-in, DocuSign integration), (3) workflows/approvals configuration; attendees procurement (Gavin Greer), legal, finance  _(due 2026-06-24; who: Zycus; open)_
- Issue vendor selection decision within 2 weeks of 24 Jun final demo (target early Jul)  _(due 2026-07-08; who: Buyer; open)_
- MSA redlining via external legal counsel (Shoosmiths) in first 2 weeks of Jul, followed by contract signature backend Jul  _(due 2026-07-25; who: Buyer; open)_

**Implicit needs** (1)
- Co-build a quantified ROI / savings model with Anna-Marie Ferguson (EB) and Gavin Greer to justify the $234k investment and de-risk any late budget challenge  _(2026-06-24)_

### HAVI Logistics GmbH  -  Havi S2C, iSource, Spend, CLM, SRM, Merlin Intake
_Owner: Dirk Fischbach  |  opp_id: 006P700000RFGL6_

**Critical moves** (5)
- Convert the 25 Jun integration demo (scheduled tomorrow per Next_Step 22 Jun) into a proof point that Zycus operationalizes AI for HAVI's strategic sourcing use cases, not only tail spend. Co-author a demo scenario with champion Mariusz and Pedro showing Merlin intake in German/English guiding a multi-supplier indirect-category sourcing event, Anna agent running pricing permutations across SLAs/term/config, and SAP ECC integration (iDoc/Z-Doc) in HAVI's landscape. Address Pedro's 1 Jun question on applicability beyond tail spend and rebuild credibility after the 'poor demo response' (8 Jun). The deal team should prep this as a rehearsed, HAVI-specific proof, not a generic walkthrough.  _(by 2026-06-25; owner: Deal team)_
- Map and activate the Economic Buyer (Magdalena Niec Finance Operations Director, Amina Struenck Director Finance DACH, or the CFO/GLT sponsor who set the cost-neutrality mandate) before the commercial close. The deal owner's manager should coordinate an executive-to-executive session to convert the cost-neutrality constraint ('no P&L impact 2026,' 21 May) into sponsorship by quantifying immediate H2 2026 Sourcing-module savings (e.g. €150K efficiency gains from Merlin-guided buying and Anna-negotiated tail-spend consolidation) and aligning it to the relevant stakeholder 2028 benchmarking credentialing goal. Without EB sign-off, the Upside forecast and 24 Jul close are not defensible.  _(by 2026-07-01; owner: Executive connect)_
- Close the pricing gap by anchoring the revised offer (submitted 19 Jun, outcome unknown) to Coupa's €300K benchmark and a phased-license model (Sourcing-only 2026, add modules 2027 as savings prove out), then quantify cost-neutrality with a named 2026 savings commitment (e.g. €150K Sourcing efficiency gains offset €XK SaaS fee in H2 2026). The deal team should present this as a risk-mitigation offer: HAVI pays only for what goes live in 2026 (Sourcing pilot Germany per 21 May plan), proves ROI, then expands. If Coupa is at €300K and Zycus cannot close to €350K-€400K with a credible 2026 payback story, Coupa wins on cost.  _(by 2026-07-01; owner: Deal team)_
- Secure SI/partner alignment to de-risk the August project start. HAVI runs a separate SI tender (23 Mar declined EY from RFP scope); the outcome and timeline are unknown. The deal team should coordinate with HAVI procurement (Dana Clauss, who is coordinating logistics per 21 May Next_Step) to confirm SI selection timeline and, if possible, position a Zycus-aligned SI (e.g. a smaller partner like AMC or Akantis from the 21 May commercial-prep discussion) as a low-risk, Germany-pilot-ready option. If SI selection slips, the August project start (7 Apr Next_Step) slips, and the close slips with it.  _(by 2026-07-08; owner: Partner)_
- Schedule and conduct the Cargolux reference call with HAVI's champion Mariusz and technical lead Pedro. The 21 May Next_Step records 'Name Cargolux as reference - Meeting to be scheduled' but no follow-up appears. Cargolux (or Pagologix, the airline reference mentioned in 21 May commercial-prep Avoma touchpoint 5) is a peer use case (logistics/supply chain, SAP ECC, Zycus since 2014 for S2C+procurement per 21 May). The deal team should coordinate the reference call within 14 days, focusing on Cargolux's AI adoption journey, SAP integration experience, and ROI realization timeline to reinforce credibility after the 'poor demo response.'  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (2)
- Run integration demo  _(due 2026-06-25; who: Zycus; open)_
- Complete preferred supplier selection  _(due 2026-06-30; who: Buyer; open)_

**Implicit needs** (2)
- Prove AI agentic capabilities work for strategic/tactical sourcing, not only tail spend, with demo in HAVI's languages/categories  _(2026-06-01)_
- Quantify immediate 2026 savings to satisfy CFO/GLT cost-neutrality mandate  _(2026-05-21)_

### Hong Kong Aircraft Engineering Company Limited  -  HAECO_HK_S2P
_Owner: Carl Kimball  |  opp_id: 006P700000NwbBd_

**Critical moves** (5)
- Carl (or deal owner's manager if Carl gets no reply within 10 business days) to re-engage HAECO immediately: identify the new Group Procurement project lead and decision-maker (Christian Pinter's current status, or his replacement), request a discovery/re-qualification meeting with the new evaluation team by mid-July 2026, and confirm whether the project continues, whether the prior evaluation progress (Zycus qualified, POC positive) will be honored, and what the revised timeline and decision process are. In the meeting, re-anchor the Zycus value narrative: ST Engineering aviation reference (same industry, same multi-entity master-data pain Zycus solved), GenAI master-data normalization validated by Charlotte in Dec 2025, Merlin/I2O agentic AI, and speed-to-value vs ERP-bundle and generalist platforms. Secure commitment to a revised RFP/POC timeline or a sole-evaluate path if the new team honors the prior qualification.  _(by 2026-07-05; owner: Deal team)_
- In the first re-engagement meeting with the new HAECO team, map the Economic Buyer and build a quantified business case collaboratively. Identify the CFO or C-level sponsor who will authorize the $450k spend and confirm the budget-approval chain. Co-develop a quantified ROI model: cost of fragmented procurement today (wasted headcount managing 7–8 ERP instances, maverick-spend leakage, audit/compliance risk exposure from manual processes), projected savings from procurement consolidation (category leverage, contract compliance, spend visibility), and payback period. Anchor the business case to HAECO's stated pain (Simon's Sep 2025 discovery call: 'Procurement strategy is not unified… decentralized tendering on manual tools… no category leverage or bargaining power') and Charlotte's master-data pain (Dec 2025: same supplier/part in different codes per entity, blocking contract and catalog consolidation). Secure EB sign-off on the business case and a commitment to the next commercial/pricing conversation by late July.  _(by 2026-07-15; owner: Deal team)_
- Deliver a refreshed ST Engineering aviation reference package to the new HAECO team: scope (multi-entity S2P, master-data consolidation, aviation compliance), master-data approach (GenAI normalization, golden-record creation, multi-ERP supplier/part mapping), aviation-compliance configuration (CAGE-code management, quality traceability, conditional UI fields for MRO/MRP requirements), and implementation timeline (kickoff to go-live). Offer a direct reference call between HAECO's new project lead and ST Engineering's procurement/IT lead to validate the fit and derisk the decision. Position this reference as proof Zycus has solved HAECO's exact pain (fragmented procurement across subsidiaries, manual processes, master-data chaos) for a peer aviation MRO customer, with faster time-to-value and lower implementation risk than GEP's generalist platform or Ariba's ERP-bundle approach.  _(by 2026-07-15; owner: Deal team)_
- Close the open Zycus deliverables (master-data team assessment of multi-ERP item-master mapping workarounds, Dow Jones integration verification) and present the findings to the new HAECO team in the re-engagement meeting or a follow-up technical session by late July. For item-master mapping, confirm whether Zycus can automate multi-ERP part-number synchronization (same part, different codes per ERP) or whether a manual consolidation step or external data-lake approach is required, and present the recommended workaround with implementation effort and timeline. For Dow Jones, confirm integration capability for sanction-list, adverse-media, and ultimate-beneficial-owner screening, or present the alternative framework (API-based custom integration). Address Charlotte's Dec 2025 master-data pain head-on and show Zycus has a concrete, implementable solution.  _(by 2026-07-25; owner: Deal team)_
- If the new HAECO team confirms the project continues and a revised timeline is set, secure a champion within the new team by late July 2026: identify the most engaged, influential, and EB-connected member of the new evaluation team (IT lead, procurement project manager, or data/analytics owner similar to Charlotte's prior role) and build a coach/advocate relationship. Share insider intelligence (what worked well in the prior evaluation, what the original team validated positively, where Zycus differentiated vs competitors), involve them in co-building the business case and ST Engineering reference validation, and secure their active advocacy in internal stakeholder alignment and vendor recommendation. A strong champion with EB access and internal influence is the highest-leverage relationship asset and the best defense against competitive displacement during the reset.  _(by 2026-07-25; owner: Deal team)_

**Open commitments** (2)
- Timeline for tender and implementation phases to be confirmed 'in coming 2 weeks'  _(due 2026-06-10; who: Buyer; overdue)_
- Engagement with new HAECO team expected before end of May 2026; C-level engagement with Carl  _(due 2026-05-31; who: Buyer; overdue)_

### IDB Invest  -  IDB Invest OppCLM Nov'25
_Owner: Grace Kim  |  opp_id: 006P700000Pe7fV_

**Critical moves** (5)
- Re-engage champion Mariana and IDB IT leadership (Julio Cesar Lima) immediately to confirm decision status and whether IDB selected Zycus or the other finalist. If no decision made, surface the internal blocker (committee delay, legal hold, budget freeze, competitor re-evaluation) and the revised decision date. If the other finalist was selected, request debrief to learn the wedge and preserve the relationship for future cycles.  _(by 2026-06-25; owner: Deal team)_
- Partner BCT must confirm MSA status with IDB (signed, in legal review, or stalled) and surface any open redlines or approval steps. If unsigned, coordinate IDB legal/procurement point of contact and expedite resolution. If signed, obtain executed copy and confirm implementation start date.  _(by 2026-06-26; owner: Partner)_
- Surface the second finalist's identity and IDB's competitive preference (Zycus leading, tied, or trailing) via BCT or Mariana. If competitor lacks AI/Merlin capability, prepare a Merlin contract-intelligence differentiation message (negotiation agent, obligation tracking, post-signature risk management) to apply in the re-engagement. If pricing is the wedge, confirm ROI vs the rival and decide whether a concession is warranted to win.  _(by 2026-06-27; owner: Partner)_
- Confirm Economic Buyer sign-off. BCT or Zycus must validate that IDB's CFO (Orlando Ferreira) or equivalent budget authority approved the $100k spend and that the approval is documented. If not confirmed, escalate to the EB directly (via Mariana introduction or Julio Cesar Lima) to secure written approval before any revised close date.  _(by 2026-07-01; owner: Partner)_
- Slip close date to realistic timeline if decision/MSA not resolved by 30 Jun. If IDB confirms selection of Zycus but MSA or EB approval is pending, propose revised close date 4-6 weeks out (late Jul/early Aug 2026) to allow contract execution, legal review, and procurement sign-off. Update forecast to Pipeline until decision and contract secured.  _(by 2026-06-30; owner: Deal team)_

**Open commitments** (2)
- IDB feedback on MSA completed 24 Apr 2026  _(who: Buyer; overdue)_
- IDB decision on finalist selection (expected week of 22 Jun per 15 Jun outreach)  _(due 2026-06-22; who: Buyer; overdue)_

### John Deere  -  JD_Merlin+AgenticAI_2025
_Owner: Arthur Raguette  |  opp_id: 006P700000KHd9V_

**Open commitments** (2)
- Phone call with Arthur to fully understand ramifications of POC pause (on hold / in-house build / competitive loss) and path forward  _(due 2026-05-25; who: Buyer; overdue)_
- 20 July re-connect call with Karen Powers to discuss priority re-assessment outcome and next steps (scheduled, upcoming in 26 days as of today 24 Jun 2026)  _(due 2026-07-20; who: Buyer; open)_

### Khansaheb  -  Khansaheb
_Owner: Dan Quinn  |  opp_id: 006P700000LtIUv_

**Open commitments** (2)
- Abraham to talk to Bilal (EB) about bringing Carl in for executive-level economic-strengthening pitch  _(who: Buyer; overdue)_
- Buyer to provide update on budget un-freeze timing and project-resume date  _(who: Buyer; open)_

**Implicit needs** (1)
- Co-author a crisis-tied ROI model quantifying margin protection and materials-cost savings enabled by procuretech investment during the blockade, to justify unfreezing the budget  _(2026-05-18)_

### Kisco Senior Living  -  Kisco Senior Living LLC_Opp_Nov25
_Owner: Steve Ovadje  |  opp_id: 006P700000PWx3N_

**Critical moves** (5)
- Direct email from the deal owner (Steve Ovadje) to Patty Rice: 'We've passed the 22 Jun decision date you shared in May — if Zycus is still in consideration I'd like to schedule a 15-minute call this week to confirm next steps and surface any blockers; if another vendor won or the project is on hold I'd appreciate knowing so we can close our side cleanly.' If no reply in 3 business days, escalate to rank-2 move.  _(by 2026-06-27; owner: Deal team)_
- Executive escalation: the deal owner's manager requests a CFO-to-CFO or exec-to-exec call with John Hanna (CFO/EB, given as ground truth in authoritative facts) to validate priority, confirm the decision timeline has slipped, and surface the real blocker (pricing gap, AP team veto, or Zip winning on a capability Zycus has not addressed). Frame: 'John, you stated on our 17 Apr demo that Kisco would decide well before September — we're now 10 weeks past that conversation and 2 days past the 22 Jun decision date Patty shared in May. I'd like 20 minutes this week to confirm where Zycus stands, and if there's a gap we can close (pricing, AP team concerns, or a capability another vendor offers) I'd like to surface it now so we can address it or step aside cleanly.'  _(by 2026-07-01; owner: Executive connect)_
- Co-build a one-page ROI model with Patty Rice and VP Finance Chrissie Ripa quantifying: (1) weekly procurement hours saved (Patty as 'team of one' handling 32 communities, goal to eliminate 'tailspin' low-value work via Merlin Intake automation), (2) AP workload reduction (team of 3 clearing exceptions in 24–48 hours, target 50% reduction via the relevant stakeholder matching per Zycus claim on 17 Apr demo), (3) maverick-spend reduction at 100% capture (current ~80% on-system per Patty 13 Nov, 20% off-system), and (4) platform-cost delta (Zycus $80k–$150k/year vs Procurement Partners ~$38k + current AP system cost). Frame total cost of ownership (TCO) including IT/AP labor saved vs split-vendor architecture (SmartPO for procurement + Automate for invoicing = two contracts, two NetSuite integrations, no unified reporting, manual reconciliation). Present to CFO John Hanna as financial justification to choose Zycus over the lower-cost alternative.  _(by 2026-07-08; owner: Deal team)_
- Surface competitive intel on Zip by asking Patty (or escalating to CFO John Hanna if Patty is unresponsive): 'What does Zip offer that has you comparing them to Zycus in the final round — is it a capability gap, pricing advantage, or stronger references?' Then position Zycus's end-to-end AI wedge (Merlin Intake conversational procurement in Teams for user adoption, AP Smartdesk AI invoice extraction for accuracy, autonomous negotiation in sourcing for tailspin reduction, single NetSuite integration vs multi-vendor fragmentation) against Zip's offering. If Zip is a point solution (e.g., invoicing-only or procurement-only), make Zip's lack of end-to-end integration and vendor-fragmentation risk the counter-narrative. If Zip offers full platform, identify the ONE capability Zycus has that Zip does not (e.g., Merlin Intake in Teams, vCard rebate structure, proven senior-living references) and make that the wedge.  _(by 2026-07-08; owner: Deal team)_
- Offer a pricing concession to close the cost gap vs SmartPO+Automate alternative: propose a 3-year contract term (vs 5-year standard, which Patty stated on 11 Feb 'Kisco does not like 5-year agreements'), lock user count at 450 eProcurement licenses with rate protection (no $3,500/50-user bundle upcharge if actual count is 600), and guarantee vCard rebate parity (confirm via vCard analysis that Kisco's current vendors are available on vCard and the rebate structure matches the 50% of AP spend they receive today). Frame as 'fast-close incentive' tied to decision by 30 Jun to preserve the Sep–Oct go-live window before Procurement Partners sunset.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (2)
- Patty Rice to follow up with next steps after 28 Apr demo  _(due 2026-05-11; who: Buyer; overdue)_
- Kisco to provide vendor selection decision after seeing '2 additional demos'  _(due 2026-06-22; who: Buyer; overdue)_

**Implicit needs** (1)
- NetSuite integration scoping (API setup for 32 separate bank accounts, one per community) with IT resource commitment and timeline  _(2026-04-17)_

### LCS (Life Care Services)  -  LCS_OPP_Aug24
_Owner: Bailey Erazo  |  opp_id: 006P700000CWvfN_

**Critical moves** (5)
- Re-engage Cory Griffiths and Elissa Rogers directly via phone and email to determine if LCS is still actively evaluating Zycus, what is blocking the RFP release (promised October 2025, December 2025, March 2026, never delivered), and whether the timeline to close by 27 August 2026 is realistic. Frame the conversation as a candid checkpoint: 'We haven't connected since the 11 September demo. Are we still in your evaluation, and if so, what do you need from us to move forward?' If no reply within 5 business days, this signals competitive displacement or project shelving.  _(by 2026-07-01; owner: Deal team)_
- If Cory or Elissa confirm LCS is still evaluating, immediately request a checkpoint meeting with Elisa Baptiste (SVP COO) or another C-level executive to validate the business case, budget authority, and executive sponsorship. Use the Brookdale and Benchmark peer success stories (senior living vertical proof points) as the executive-connect wedge. The agenda: confirm the business problem, quantify the ROI (savings, cycle time, manual effort reduction), map the Economic Buyer and approval chain, and co-author a mutual close plan with dated milestones (RFP response, shortlist, commercial rounds, contract). Without an engaged Economic Buyer, the deal cannot progress to negotiation or close.  _(by 2026-07-08; owner: Executive connect)_
- Run a competitive review and repositioning session with the buyer (if they re-engage) to address the five-vendor shortlist (Coupa, iValua, JAGGAER, SAP Ariba, iCertis) and determine Zycus's current standing. Request a debrief: 'You mentioned in October 2025 you were evaluating other vendors. Where does Zycus stand today in your shortlist, and what are the key decision criteria we need to address?' Activate the Merlin agentic AI differentiation (autonomous sourcing and negotiation vs competitors' legacy UX), integration flexibility (API connectors to Oracle, SAP, Dynamics per the September demo), and senior living peer validation (Brookdale, Benchmark) as the win wedge. Without knowing which vendor(s) are leading, the deal is competitively blind.  _(by 2026-07-08; owner: Deal team)_
- If the buyer re-engages and confirms interest, co-develop a quantified ROI model with LCS finance stakeholders (Robbie Rushing, Sr. Finance Manager, or David Bennett, Director Accounting) that ties Zycus S2P consolidation to their stated pain points: eliminating multi-vendor fragmentation (DDSI and others), reducing manual QA repetition, enabling multi-client/multi-entity support, and flexible data import for external members. Quantify savings (vendor consolidation, manual effort reduction, cycle time improvement) and anchor to their ~$150k+ budget range. Present the ROI model to Elisa Baptiste (SVP COO) or the Economic Buyer as the business case for executive sign-off. Without a quantified value case, the deal lacks the financial justification to close.  _(by 2026-07-24; owner: Deal team)_
- If no buyer reply to rank-1 outreach within 5 business days (by 1 July 2026), escalate to Elisa Baptiste (SVP COO) via the deal owner's manager (executive connect) with a candid message: 'We delivered a demo in September 2025 and have been awaiting your RFP since October. We want to ensure Zycus is still a fit for LCS. Can we schedule 15 minutes this week to confirm your timeline and next steps, or should we close out the opportunity?' If no reply within 7 days of this escalation (by 8 July 2026), recommend disqualifying the deal and downgrading forecast to Omitted. A 9+ month silence with repeated RFP delays and no Economic Buyer engagement signals the project is shelved or lost to a competitor; continuing to forecast it is speculation.  _(by 2026-07-08; owner: Executive connect)_

### Mair Group  -  Mair Group S2P
_Owner: Dan Quinn  |  opp_id: 006P700000PtQGP_

**Open commitments** (1)
- Conduct SOW workshop onsite in Abu Dhabi (Hrishi +1 & Dan)  _(who: Zycus; open)_

### Manscaped  -  Manscaped_P2P_Mar26
_Owner: Steve Ovadje  |  opp_id: 006P700000VFXEl_

**Open commitments** (4)
- Schedule and deliver follow-up demo covering eProcurement and Insights Studio modules  _(who: Zycus; overdue)_
- Prepare and deliver modules-vs-pricing breakdown document for buyer review  _(who: Zycus; overdue)_
- Submit RFP response for new Procurement-only RFP  _(due 2026-07-16; who: Zycus; open)_
- Respond to updated pricing shared 2026-06-09 for reduced Procurement-only scope  _(due 2026-06-18; who: Buyer; overdue)_

**Implicit needs** (2)
- Quantify the operational cost of email-based decentralization and tail-spend leakage to build ROI case for Economic Buyer sign-off  _(2026-05-12)_
- Map and validate the Economic Buyer and approval chain before commercial proposals advance  _(2026-05-12)_

### McAfee, LLC  -  McAfee ANA
_Owner: Karson Keogh  |  opp_id: 006P700000JM8aH_

**Critical moves** (5)
- Map and engage the Economic Buyer (CFO or CPO) directly this week before the internal CFO meeting (24 Jun) and CEO/CIO ANA video review (week of 24 Jun) — position the autonomous negotiation ROI case (4–7% tail-spend savings, 70–80% cycle-time reduction, buyer capacity freed for strategic work) at executive level to convert internal reviews into executive buy-in and budget approval, and secure EB sponsorship before POC down-select  _(by 2026-06-27; owner: Executive connect)_
- Pin down the POC down-select decision date and success-factor scorecard with Aitor immediately after the Nippon Gas reference debrief (24 Jun) and CEO/CIO meeting (week of 24 Jun) — request a checkpoint to gauge Zycus's competitive standing vs. the unnamed POC rival(s) and formalize the vendor selection timeline so contract negotiation can begin without delay  _(by 2026-06-27; owner: Deal team)_
- Initiate MSA/SOW scoping and legal review kickoff NOW in parallel with POC validation (do not wait for down-select) — surface contract cycle timeline (legal, InfoSec, procurement approval likely 3–4 weeks) with Aitor and propose a phased go-live or extended close date (late Jul/early Aug) anchored to contract reality, protecting deal momentum while setting realistic execution expectations  _(by 2026-06-30; owner: Deal team)_
- Deliver the formal commercial proposal for Anna module (pricing tiers, integration cost range) requested 21 Oct 2025 before the POC down-select — tie pricing to the ROI model (4–7% savings on tail spend, 70–80% cycle-time reduction) and benchmark against buyer's stated threshold ('if the product is more expensive than the savings obtained, we're not going to proceed') to justify investment and pre-empt competitor undercutting  _(by 2026-06-30; owner: Deal team)_
- Reconstruct the competitive shortlist and the unnamed POC rival's positioning by asking Aitor directly after the 24 Jun reference debrief: who is the other vendor(s) in the POC, what is their pricing/proposal, and where does Zycus stand on the success-factor scorecard — use the intel to sharpen differentiation (S2P scalability, MS Teams integration, ROI model) and address any competitive gaps before final evaluation  _(by 2026-07-05; owner: Deal team)_

**Open commitments** (4)
- Alexa to share executive note to Nick (Head of Global Procurement)  _(who: Zycus; open)_
- Debrief with the relevant stakeholder reference (Tania) on Tuesday 24 Jun  _(due 2026-06-24; who: Buyer; open)_
- Internal CEO/CIO meeting to review ANA video with key stakeholders, week of 24 Jun  _(due 2026-06-28; who: Buyer; open)_
- CFO meeting on Monday (likely 24 Jun)  _(due 2026-06-24; who: Buyer; open)_

### Mizuho Bank, Ltd.  -  Mizuho Americas 2024
_Owner: Edward Dlugosz  |  opp_id: 006P7000009T3v1_

**Critical moves** (5)
- Secure a 1:1 or small-group executive call with George Andrus (Decision Maker, Head of Procurement) to (a) confirm Zycus remains preferred vendor post-pause, (b) understand global framework timeline and decision authority (Americas-led vs Japan HQ approval), and (c) align on restart process and Zycus's role in validating the framework (e.g. proof-of-concept in one region, workshop with global procurement leads)  _(by 2026-07-01; owner: Executive connect)_
- Co-author a quantified ROI model with Alex Jaffee and Amit Saraff (operational champions) tying Mizuho's addressable spend ($750M-$1B), contract volume (1,500 new contracts/year), and invoice count (15,000/year) to measurable cost avoidance (cost per invoice reduction, sourcing-cycle-time savings, AP FTE reduction, fraud/duplicate prevention), using PwC and Heineken deployments as benchmarks, and deliver it to George Andrus and Lee Tenny by end of Jul 2026 to support global framework business-case discussion  _(by 2026-07-31; owner: Deal team)_
- Deliver the four open action items from 09 Dec 2025 AP session before the pause restart discussion begins: (a) confirm support for international bank-account validation in vendor onboarding and third-party integrations, (b) provide sample invoice rejection report and dashboard, (c) send documented list of Zycus AI functionalities, (d) clarify SAP invoice processing vs Zycus transition options and integration requirements via follow-up documentation. Bundle these into a single comprehensive AP readiness package and send to Alex Jaffee, Amit Saraff, and Yolanda Ferrigno (Head of AP) by mid-Jul 2026.  _(by 2026-07-15; owner: Deal team)_
- Research and send summary report on Zycus's Japan market penetration and clients using the platform in native Japanese-based environments (e.g. MUFG regions, Panasonic North America evaluation mentioned by Edward Dlugosz on 17 Jun call, any other Japan-headquartered clients) to Lee Tenny, George Andrus, Matthew Brzoza, and Alex Jaffee, emphasizing multi-region deployment capability and Japan HQ acceptance of Zycus in other geographies  _(by 2026-07-15; owner: Deal team)_
- Provide periodic updates (monthly starting Jul 2026) to Lee Tenny, George Andrus, Matthew Brzoza, and Alex Jaffee on Zycus company and product news, including new banking/financial-services clients (especially in Asia, Japan, or multi-region deployments), Merlin AI enhancements (I2O agentic negotiation agent, next-gen invoice/vendor onboarding AI), and relevant Horizon user-group event highlights (PwC, Heineken presentations if videos are shareable). When significant material updates arise (e.g. new APAC bank client win, major Merlin AI release), ping Matthew Brzoza and Alex Jaffee directly instead of waiting for monthly cadence.  _(by 2026-07-20; owner: Deal team)_

**Open commitments** (6)
- Research Zycus's penetration into Japanese market and clients using platform in native Japanese-based environments, send summary report to Lee, George, Matthew Brzoza, and Alex  _(who: Zycus; open)_
- Send detailed information about upcoming Zycus user group event (Horizon, dates/location/agenda) to Lee and George  _(who: Zycus; open)_
- Provide periodic updates to Lee and George on company and product news, including new clients and deployments in Asia, as material developments occur  _(who: Zycus; open)_
- Inform Lee and George of name of new Middle East client once publicity clause allows public disclosure  _(who: Zycus; open)_
- When significant material updates arise (new client wins, major product changes), ping Matthew Brzoza and Alex Jaffee directly instead of waiting for next monthly check-in  _(who: Zycus; open)_
- Review Zycus Horizon user group event information and evaluate whether to attend  _(who: Buyer; open)_

### MTR Corporation Limited  -  MTR_HK_CLM
_Owner: Carl Kimball  |  opp_id: 006P700000KTTO5_

**Open commitments** (1)
- Respond to RFP released 9 Jun 2026 and reach out to MTR project lead  _(who: Zycus; open)_

### NVISION Eye Centers  -  NvisionEyeCenter_S2P_May26
_Owner: Steve Ovadje  |  opp_id: 006P700000Xmqu9_

**Open commitments** (2)
- Provide ROI / business-case form to Kristopher for internal budgeting analysis  _(due 2026-06-26; who: Zycus; open)_
- Deliver hour-long demo focusing on Merlin intake (Teams), iSource, ANA, supplier mgmt, eProcurement  _(due 2026-06-25; who: Zycus; open)_

**Implicit needs** (2)
- Validate that Zycus iSource supports multi-supplier sourcing events with substitute-product equivalents (multiple models within one RFQ) and section-level supplier visibility  _(2026-06-15)_
- Confirm integration or coexistence with existing medical-supplies tool (NV/Envy) so eProcurement rollout does not break current accounting/invoicing workflows  _(2026-06-15)_

### Omnia Holdings Limited  -  OmniaS2C24
_Owner: Caroline Lacocque  |  opp_id: 006P7000009O2Ri_

**Critical moves** (5)
- Confirm with Tracy (Head of Legal) that legal sign-off on the final redlined SOW (due from Marianne 24 Jun) will be completed by EOD 26 Jun, and coordinate a final alignment call on 25 or 26 Jun with Tracy, Marianne, and Zycus legal (Sohab) to clear any last redline before signature so no legal back-and-forth blows the 30 Jun close window.  _(by 2026-06-26; owner: Deal team)_
- Chase the relevant stakeholder plan signature status (noted 7 May as 'also signing their plan this week' but not updated since) and clarify in writing whether Accenture countersignature is a dependency for Omnia's MSA signature or go-live kickoff; if yes, escalate to Accenture leadership to secure signature by 27 Jun; if no, confirm scope in the close-out email to remove ambiguity.  _(by 2026-06-27; owner: Deal team)_
- Lock the design-document sample share (Hrishikesh committed 17 Jun, discussed 23 Jun) and the integration-framework exchange (Dieter to provide Omnia standards, Hrishikesh to share Swagger API link, follow-up session this week per 23 Jun) as post-signature kickoff prerequisites in the MSA close-out email, so Omnia's confidence in design-document enforceability stays high and no buyer's-remorse surfaces between signature and project start.  _(by 2026-06-28; owner: Deal team)_
- Coordinate Zama (Financial Systems Support Analyst) to finalize Certinal e-sign contracting and submit the data/design-authority paperwork to the relevant stakeholder/finance by 27 Jun so e-sign integration is ready for MSA countersignature and does not become a post-signature blocker for contract execution or go-live.  _(by 2026-06-27; owner: Deal team)_
- Schedule the integration deep-dive session (Dieter + Omnia IT + Hrishikesh + Zycus integration team) for the week of 1 Jul (post-signature) to align on event-based API patterns, security (SSO via the identity provider, encrypted MFT for contract migration), and open API specs per Omnia's framework, so design workshops can start immediately after MSA signature without integration-architecture rework.  _(by 2026-07-05; owner: Deal team)_

**Open commitments** (4)
- Marianne to finalize and submit redlined SOW (cleaned, approved by Tracy/legal) to Zycus  _(due 2026-06-24; who: Buyer; open)_
- Dieter to share Omnia's integration standards/framework document with Zycus and arrange follow-up integration session  _(due 2026-06-27; who: Buyer; open)_
- Hrishikesh to share Swagger API link for Zycus APIs with Omnia integration team  _(due 2026-06-27; who: Zycus; open)_
- Caroline (deal owner) and Zycus legal to follow up on Certinal e-sign contracting and any pending approval items  _(due 2026-06-27; who: Zycus; open)_

**Implicit needs** (2)
- Route all integrations through Omnia's middleware (OIB on Azure) with encrypted endpoints so Omnia retains control and security compliance  _(2026-06-23)_
- Ensure Merlin contract-discovery extracts metadata from legacy PDFs during migration at ≥85% accuracy for custom fields so Omnia can validate and merge into supplier master without re-keying  _(2026-06-23)_

### PDI Technologies  -  PDI Tech Opp - Feb '26
_Owner: Grace Kim  |  opp_id: 006P700000TVIUf_

**Critical moves** (5)
- Re-engage Scott Fields directly (call + email) to diagnose the 48-day silence: ask explicitly whether he is stuck building the internal ROI business case, waiting on Finance budget timing, or if procurement spend-analytics has been de-prioritized. If stuck, offer to co-author a Finance EB business case (tail savings ROI model, year-one platform payback via Anna autonomous negotiation) and co-present it to the CFO or VP Finance (Nicole Wu, Scot Crawford) to unlock the unfunded budget. If de-prioritized, surface the opportunity cost: every month without Zycus he continues manual vendor analysis via spreadsheets and foregoes tail-spend savings. Set a binary outcome: EB meeting within 10 business days or disqualify.  _(by 2026-07-01; owner: Deal team)_
- Map and engage the Economic Buyer (Finance leadership: CFO Nicole Wu or VP Finance Scot Crawford) within 14 days by requesting a joint business-case review call with the relevant stakeholder it as a Finance partner collaboration to validate the ROI model (vendor consolidation savings, tail-spend automation payback, year-one platform cost recovery), not a sales pitch. If Scott resists or delays the EB introduction, that is a disqualify signal — he may lack internal influence or budget authority is not real. The EB meeting is the gate: without it, the deal cannot progress past Pipeline.  _(by 2026-07-08; owner: Deal team)_
- Expand the stakeholder map beyond Scott Fields (sole contact, no multi-thread) by identifying and engaging at least one Finance stakeholder (Nicole Wu CFO, Scot Crawford VP Finance) and one IT stakeholder (Seth Wegner Director IT, Pallavi Muthyala IT Project Manager) who will own platform integration, user provisioning, and data feeds (Netsuite, contract repository). Request Scott's permission to brief them on the solution scope and implementation plan, framing it as de-risking his internal rollout and building organizational air cover. If Scott blocks expansion ('I'll forward the invite if needed'), that signals weak internal influence or lack of real organizational priority — flag as disqualify risk.  _(by 2026-07-08; owner: Deal team)_
- Build and deliver a quantified ROI model (co-authored with Scott if he re-engages, or standalone if he remains silent) showing year-one tail-spend savings payback via Anna autonomous negotiation module. Use Scott's stated spend scale (~$60M influenceable, ~$12–15M tail per 18-Feb discovery) and conservative tail-savings assumptions (e.g. 5–8% via automated negotiation = $600k–$1.2M year-one impact) to demonstrate platform cost recovery within 12 months. Present this to the relevant stakeholder (Nicole Wu, Scot Crawford) as the business case justifying the unfunded budget allocation. If Scott re-engages, co-present. If he remains silent, deliver it directly to Finance with a note that Scott requested it but is underwater with other priorities (positioning Zycus as the solution to his capacity constraint).  _(by 2026-07-08; owner: Deal team)_
- If Scott re-engages and confirms continued interest, immediately schedule a Merlin + Anna live proof-of-concept (POC) session using a sample of PDI's actual messy Netsuite/spreadsheet vendor data (which Scott has said is scattered and incomplete). Demonstrate Auto Class AI normalizing and classifying their real data, Spend Miner surfacing actual vendor consolidation opportunities in their categories, and Anna running a simulated tail-spend negotiation event. Invite Scott plus the new team member (Mankeet, mentioned 23-Apr) and ideally one Finance or IT stakeholder. A hands-on POC with their data accelerates conviction, validates ROI assumptions, and creates internal proof points Scott can use to sell the EB. If Scott declines the POC or cannot provide sample data, that signals low prioritization — disqualify.  _(by 2026-07-15; owner: Deal team)_

**Open commitments** (1)
- Review quote and provide feedback or decision  _(who: Buyer; overdue)_

### Pep Promotions Execution Partners, LLC  -  Pep Opp - December '25
_Owner: Grace Kim  |  opp_id: 006P700000QqTmw_

**Critical moves** (5)
- Re-engage Christina Behm with a forcing question: what specifically is blocking the on-site visit promised since 2026-02-18, who needs to approve it, and can we bring Tim Drost or the approval committee into a brief checkpoint call this week to validate project priority, approval status, and realistic timeline. If she cannot mobilize within 7 business days, escalate directly to Tim to confirm the deal's urgency.  _(by 2026-07-01; owner: Deal team)_
- Map the Economic Buyer and approval committee by asking Christina or Tim to walk through the approval process: who controls budget sign-off, who sits on the approval gate Christina referenced on 2026-05-26, what the approval criteria are, and what the timeline is. Request a brief checkpoint call with the approval committee or Economic Buyer to validate Zycus fit and address any unspoken concerns.  _(by 2026-07-01; owner: Deal team)_
- Build and present a quantified ROI model showing manual-hour savings (current FTE burden on manual document follow-ups, exception tracking, COI/MSA expiry management), compliance-risk reduction (aggregate liability exposure from untracked exceptions), and total-cost-of-ownership advantage vs the internal build (Zycus 10-20 week implementation + subscription cost vs internal IT multi-quarter build + ongoing maintenance). Position Merlin AI contract insights and exception reporting as non-trivial capabilities that are expensive and time-consuming to build and sustain in-house.  _(by 2026-07-08; owner: Deal team)_
- Convert the stalled on-site visit into a virtual executive briefing with Tim Drost and the approval committee, presenting a tailored demo of exception-tracking reporting and contract-term visibility (the specific gaps Christina and Jillian cited on 2025-12-17) plus the quantified ROI model. Frame it as a final validation before commercial discussions.  _(by 2026-07-08; owner: Deal team)_
- If Christina or Tim confirm the internal-build option is winning or the project is deprioritized, qualify out gracefully and propose a future re-engagement trigger (e.g., when their IT team completes the build scoping or if the build timeline slips beyond Q3 2026). Do not chase a ghost deal past 2026-07-15.  _(by 2026-07-15; owner: Deal team)_

**Open commitments** (2)
- Provide update on internal approvals and next steps by end of June 2026  _(due 2026-06-30; who: Buyer; overdue)_
- Confirm or decline tentative 3rd-week-July connect proposed by Grace on 2026-06-15  _(due 2026-07-15; who: Buyer; open)_

### Publicis Groupe  -  Publicis - CLM and Request Management
_Owner: Pierre Meraud  |  opp_id: 006P700000Xl06R_

**Critical moves** (5)
- Escalate through Florence (CPO, champion) to unlock the technical validation layer and secure the 90-minute workshop date within 7 days, then convert workshop output to a signed SOW (OF2) within 10 days to protect the July project start.  _(by 2026-06-30; owner: Deal team)_
- Drive legal MSA redlines to closure within 10 days in parallel with technical workshop; confirm legal sign-off and final MSA draft by 2026-07-05 to leave time for signature before 2026-07-13 close.  _(by 2026-07-05; owner: Deal team)_
- Co-author a one-page value case with Florence or Emeline anchoring the deal to a concrete savings or efficiency target (cycle-time reduction, cost savings, compliance improvement) before contract signature to ensure executive alignment and protect against post-signature budget cuts or delays.  _(by 2026-07-08; owner: Deal team)_
- Secure written confirmation from Publicis (Florence or Emeline) of the answers to the additional questions they were expected to return with following the 2026-06-02 presentation, to validate alignment of Florence's vision with Publicis' contractual processes before contract signature.  _(by 2026-07-01; owner: Deal team)_
- Add buyer-side contact roles to the opportunity (Emeline Le Foyer as Champion, Cedric Elmaleh as Influencer, technical validation leads and legal as Influencers) to document the full stakeholder map and support multi-threaded engagement tracking.  _(by 2026-06-28; owner: Deal team)_

**Open commitments** (5)
- Send new OF1 to answer Emeline's requests to reduce budget for FY26  _(due 2026-06-05; who: Zycus; overdue)_
- Send OF2 (SOW) following workshop  _(due 2026-06-15; who: Zycus; open)_
- Publicis to return with additional questions following 2026-06-02 presentation  _(due 2026-06-08; who: Buyer; overdue)_
- Schedule 90-minute technical workshop to finalize SOW (OF2)  _(due 2026-06-12; who: Buyer; overdue)_
- Legal to send first remarks on MSA  _(due 2026-06-17; who: Buyer; overdue)_

### Robert Bosch GmbH  -  Bosch S2P
_Owner: Dirk Fischbach  |  opp_id: 006P700000PlMpu_

**Critical moves** (5)
- Re-engage the buyer contact directly to confirm second RFP round timeline, implementation partner selection progress, and commercial discussion kickoff. The last buyer touch was 2 Jun (22 days ago); the rep-initiated outreach 22 Jun ('call on 25.6') has received no reply. With 37 days to close and vendor award expected in July, a 3-week silence post-POC is a stall. The play: the deal owner calls the Category Purchasing contact (the evaluation lead on the closing call 22 May) this week to say 'Following up on our POC close and the integration/demand-mgmt deep-dives we scheduled, I want to make sure we're aligned for the second RFP round you mentioned for end-June. Can we schedule 30 minutes this week to review your findings, address any outstanding questions from the technical deep-dive (BYOK/SCIM, API docs), and confirm next steps for the commercial discussion in June? I want to ensure Zycus is positioned to support your July award timeline.' If no reply in 5 business days, escalate to the deal owner's manager for an executive check-in.  _(by 2026-06-27; owner: Deal team)_
- Map the competitive shortlist and build a differentiation battle card anchored to AI/Merlin innovation and S2P unification. The Next_Step__c (2 Feb) confirms 'long-list of 4-5 suppliers,' but no competitor is named and no scoring rubric is visible. The POC closing call (22 May) made no mention of other vendors or competitive comparison. Without knowing who Zycus is up against and how Bosch will score the second RFP round (end-June), the wedge is a guess. The play: on the re-engagement call (rank-1 move), ask the buyer 'Who else is in the final round, and what are the top 3 scoring dimensions for the July award?' Then build a competitive matrix highlighting Zycus strengths (organic AI DNA vs. bolt-on acquisitions, S2P unification vs. best-of-breed fragmentation, partnership on AI roadmap) against the named rivals' weaknesses. Tailor the follow-up deep-dives (integration, demand-mgmt) to expose competitor gaps Bosch has flagged.  _(by 2026-07-05; owner: Deal team)_
- Deliver the written BYOK/SCIM commitment and the API documentation (Swagger, Postman collections) by 30 Jun and proactively schedule a follow-up call with the relevant stakeholder/security leads to walk through the implementation plan. The POC closing call (22 May) flagged 'BYOK/SCIM not available' and 'lack of API documentation' as critical deviations. The Zycus VP committed 'written confirmation by next week' (~26 May), and the technical deep-dive (26 May) committed to share Swagger/Postman offline — but as of 24 Jun, neither deliverable is confirmed. If these gaps remain open when Bosch scores the second RFP (end-June), they will drag technical-fit vs. rivals who may support BYOK/SCIM out of the box. The play: send the written BYOK/SCIM roadmap and API documentation to the technical contacts (Sabina Nordmann, the Business Team Lead IT) by 30 Jun, copy the evaluation lead, and offer a 1-hour call to demo the BYOK encryption flow and walk through the Swagger/Postman examples. Turn the deviation into a differentiator by showing faster roadmap execution than the competition.  _(by 2026-06-30; owner: Deal team)_
- Secure a champion post-POC who will advocate for Zycus through the July award decision. Across 28 buyer touchpoints, no Bosch contact demonstrated active advocacy (introductions, pushing back competitors, driving the agenda). The most engaged contacts (Sabina Nordmann, the Category Purchasing evaluation lead, Andre Hoepfinger) are coaches and evaluators, not champions. In a 4-5 vendor RFP with $1.2M at stake and no active EB engagement, Zycus needs an internal sponsor who will sell the platform upward to the CFO/C-level. The play: on the June commercial discussion (or the re-engagement call, rank-1 move), identify which contact has the most to gain from Zycus winning (career, budget, transformation mandate) — likely the Portfolio Owner for IT & Processes (Andre Hoepfinger) or the evaluation lead — and equip them with a board-ready business case (quantified ROI, risk mitigation, AI roadmap) they can present internally. Ask 'What would make you the hero of this procurement transformation?' and co-build the executive pitch. Partner with the champion to pre-wire the July award by aligning Zycus strengths to the EB's priorities (cost reduction, AI innovation, compliance).  _(by 2026-07-08; owner: Executive connect)_
- Prepare and deliver a concise post-POC executive summary for the Economic Buyer (once identified) that translates the POC findings into a board-ready business case: quantified ROI (cost savings, cycle-time reduction, headcount efficiency), risk mitigation (compliance, ESG, supplier risk), and AI roadmap co-innovation. The POC closing feedback (22 May) confirmed strong functional fit but flagged 'huge potential for customization and implementation effort.' The EB will want to know: what is the total cost of ownership (SaaS + implementation + customization), what is the payback period, and what is the risk of a 12-18 month deployment? The play: the deal owner and the deal owner's manager co-author a 2-page executive summary anchored to Bosch's stated pain (fragmented tools, manual sourcing, no AI-driven guided buying) and the POC validation ('platform passed majority of use cases at least partially'). Include a phased deployment plan (Phase 1: S2C + Merlin intake, Aug 2026 start, Jan 2027 go-live; Phase 2: P2O + supplier mgmt, Q2 2027; Phase 3: AI expansion, Q4 2027) with milestone-based ROI (20% cycle-time reduction in Phase 1, 30% tail-spend automation in Phase 2, etc.). Send to the champion (once secured, rank-4 move) to present internally before the July award.  _(by 2026-07-15; owner: Executive connect)_

**Open commitments** (2)
- Written commitment on BYOK and SCIM architecture  _(due 2026-05-26; who: Zycus; overdue)_
- Second round of RFP to start by end of June; implementation partners to be contacted in parallel  _(due 2026-06-30; who: Buyer; open)_

**Implicit needs** (2)
- Detailed API documentation and versioning to support integration validation  _(2026-05-22)_
- Live demonstration of data import/export functionalities for migration assessment  _(2026-05-22)_

### Roivant Sciences Ltd  -  Roivant/Immunovant_S2P_RFP_OPP
_Owner: Justin Ajmo  |  opp_id: 006P700000VM8BZ_

**Critical moves** (5)
- Propose a joint NetSuite-integration scoping workshop (Zycus implementation team + Roivant IT + NetSuite partner if applicable) to unblock the buyer's stated internal NetSuite decision. Position Zycus as the active partner solving their blocker, not a passive finalist. Offer to facilitate a half-day working session to map NetSuite instance scope, API touchpoints, master-data sync, and integration timeline, converting the buyer's internal dependency into a Zycus-led next step.  _(by 2026-06-28; owner: Deal team)_
- Map and engage the Economic Buyer (likely CIO Ian Rosenblum or CFO) via a tri-party executive briefing (Procurement lead Michelle + IT/CIO + Finance) to validate budget, timeline, and Coupa-replacement forcing function. Present a one-page ROI summary (cycle-time reduction, compliance automation, NetSuite integration ROI) and a mutual close plan anchored to the buyer's Coupa contract-expiration date.  _(by 2026-07-05; owner: Executive connect)_
- Re-engage Jason (project lead facilitating IT knowledge transfer) to understand the incoming IT leadership's priorities, vendor preferences, and timeline for taking ownership of the S2P project. Offer to present a tailored briefing for the new IT leader positioning Zycus as the continuity choice (already evaluated, finalist-selected, NetSuite-fit confirmed) vs forcing a vendor re-evaluation that delays their Coupa replacement.  _(by 2026-07-08; owner: Deal team)_
- Deliver detailed written responses to Daniel Pires's and Jessica Craft's open questions from the 20 Apr demo (committed as follow-up action items but not yet closed per available evidence), and propose a targeted 30-minute Q&A call to close any remaining technical gaps before pricing/commercial discussions. Use this as a re-engagement forcing event if buyer has not responded to rank-1 workshop proposal.  _(by 2026-06-30; owner: Deal team)_
- Request competitive intelligence from Michelle or Jason: who is the other top-two finalist, what is their positioning, and what is the buyer's evaluation/selection timeline and process from here (pricing review, finalist presentations, EB approval, contracting). Use this intel to refine positioning and accelerate next steps.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (2)
- Resolve internal NetSuite decision (blocking next steps per 23 Jun Next Step)  _(who: Buyer; open)_
- Complete IT leadership knowledge transfer (Jason facilitating) before moving forward with next steps  _(who: Buyer; open)_

### S&C Electric Company  -  S&C Electric_S2C_May 2024
_Owner: Richard Hunsinger  |  opp_id: 0066700000ztGNF_

**Critical moves** (5)
- Execute flawless module-by-module demos 2026-06-22 to 2026-06-30 with clear differentiation on Merlin AI, unified platform architecture, and indirect eProcurement + SIM depth to out-score Oracle and the unknown third vendor on the 217-deliverable common scorecard. Assign best presenters to each module, rehearse integration proof points (Oracle EBS, DocuSign CLM, Siemens 3DX version control and data retrieval per 2026-05-14 request), deliver sandbox environment (overdue per 2026-05-14), and align demo flow to S&C's requirement order per Katie's request. This is the highest-leverage window to win the deal.  _(by 2026-06-30; owner: Deal team)_
- Deliver final RFP response by 2026-07-18 with a crisp, credible phase-two direct-materials roadmap that formalizes the partner/Appextend solution for PLM integration, complex BOM ingestion, and Oracle MRP min/max ordering. Include partner identification (if applicable), Appextend development approach, fixed-cost estimates, and timelines per Hunsinger's 2026-06-03 commitment. Frame direct-materials as a growth path, not a gap, and demonstrate Zycus can support S&C's long-term needs without forcing a second vendor selection. Coordinate with the product/development team and Ronit to finalize the scope statement per Next Step 2026-06-18 ('Ronit to finalize new Direct components, confirm greatly minimized focus on direct').  _(by 2026-07-18; owner: Deal team)_
- Secure executive engagement session with Jim Boss (EB, VP Global Operational Excellence, Executive Sponsor) and Kara Weiner (VP Supply Chain, sponsor driving 2026 implementation) within 7 days of demo completion (target early July, before final down-select). Position as a strategic partnership discussion focused on long-term procurement transformation vision, ROI and productivity-gain business case, and phase-two roadmap alignment. Bring an executive-level deck (not a vendor pitch) and elevate Yufeng's advocacy by aligning her with executive sponsorship. This is the missing lock: demos win the scorecard, but executive buy-in locks the strategic fit and overrides IT/Oracle preference.  _(by 2026-07-10; owner: Executive connect)_
- Provide the three customer references requested per Next Step 2026-06-23 with one from each implementation profile: (1) smooth implementation (best-case reference), (2) some issues encountered and resolved (honest reference showing issue resolution), and (3) problematic implementation with lessons learned (credibility reference). Coordinate with customer success to identify the right accounts, prep the references on S&C's evaluation focus (SIM + indirect eProcurement, AI/Merlin, Oracle integration, phase-two roadmap), and deliver contact details to Katie Booth and Yufeng Chen by 2026-07-10 to allow time for reference calls before final down-select.  _(by 2026-07-10; owner: Deal team)_
- Deliver the overdue sandbox test environment immediately (due 2026-06-22 per 2026-05-14 request, now 2 days overdue) to allow S&C SMEs pre-demo and post-demo hands-on validation of integration capabilities (Oracle EBS, DocuSign CLM, Siemens 3DX) and Merlin AI functionality. Coordinate with Ronit and the technical team to provision the sandbox with sample data, demo scenarios, and integration connectors, and share access credentials with Katie Booth, Dan Chao, and Yufeng Chen by 2026-06-26 (2 days before first demo). Frame it as a 'try before you buy' experience that gives SMEs confidence in the platform before scoring the demos.  _(by 2026-06-26; owner: Deal team)_

**Open commitments** (7)
- Provide three customer references for implementation levels: smooth, some issues, problematic  _(due 2026-07-18; who: Zycus; open)_
- Finalize new Direct components scope (confirm greatly minimized focus on direct materials) and formalize in final RFP response  _(due 2026-07-18; who: Zycus; open)_
- Provide sandbox test environment for pre-demo and post-demo hands-on validation of integration capabilities  _(due 2026-06-22; who: Zycus; overdue)_
- Demonstrate integration proof with the relevant stakeholder, DocuSign CLM, and Siemens 3DX showing version control and data retrieval during demos  _(due 2026-06-30; who: Zycus; open)_
- Clarify planning integration, 3DX connectivity, and direct material support gaps (including API/supplier portal details) and provide updated clarity  _(due 2026-06-09; who: Zycus; overdue)_
- Reply to rep outreach on final requirements match meeting scheduled 2026-06-23 and confirm meeting date/time  _(due no due date; who: Buyer; open)_
- Attend and complete vendor demos during scheduled window  _(due 2026-06-30; who: Buyer; open)_

**Implicit needs** (1)
- Send detailed gap list on direct-materials capability (PLM, BOM, MRP) and phase-two partner/Appextend solutions (committed by Zycus, delivered 2026-06-04 per call)  _(2026-06-03)_

### SAMI  -  SAMI_S2P
_Owner: Mohamad Alhakim  |  opp_id: 006P700000RD9Ir_

**Critical moves** (5)
- Identify and map the Economic Buyer and Decision Maker. In the 2026-06-23 demo or immediately after, ask Franck or Yousef: who controls the commercial decision and budget approval for this $2.2M procurement, and who makes the final vendor-selection decision? Request an introduction to that person (likely SAMI VP Procurement, CFO, or C-level sponsor) within 7 days. Without EB/DM identification and alignment, the deal will stall in procurement limbo or be overruled by a senior leader who favors a competitor.  _(by 2026-07-01; owner: Deal team)_
- Deliver the three unresolved blockers: (1) exhaustive list of aerospace and defense OEMs using Zycus SRM with usage metrics (contact the relevant stakeholder for Boeing, Lockheed Martin, Northrop Grumman, BAE Systems, or regional defense OEMs if available); (2) formal written response on on-prem vs cloud deployment model acceptability, including hybrid/sovereign-cloud options if pure cloud is disqualifying; (3) detailed data center location and InfoSec compliance documentation for Saudi defense-sector requirements (ISO 27001, SOC 2, regional data residency, sovereign compliance). Deliver all three to Franck and Yousef by 2026-07-05. If any blocker is a no-go (e.g. Zycus cannot meet on-prem requirement), surface it immediately to the deal owner's manager for escalation or deal qualification decision.  _(by 2026-07-05; owner: Deal team)_
- Make Merlin agentic AI the centerpiece of the 2026-06-23 SRM demo. Position Merlin supplier negotiation agent, contract intelligence, and intake automation as the Phase 2 procurement-transformation unlock after S2P foundation is deployed. Quantify ROI vs manual procurement workflows (cycle-time reduction, cost savings, headcount efficiency) using a defense-sector or large-enterprise reference if available. Contrast Zycus AI-first architecture against GEP's weaker AI story and Coupa/Ariba legacy systems. Confirm SAMI's AI appetite and use-case fit during or immediately after the demo. If SAMI is AI Hungry and engages deeply, make Merlin the commercial wedge and price it as a differentiated Phase 2 upsell. If SAMI is AI Curious or Resistant, pivot to functional S2P fit and EY partnership strength.  _(by 2026-06-23; owner: Deal team)_
- Identify and develop a champion inside SAMI. In the 2026-06-23 demo or immediately after, ask Franck or Yousef: who inside SAMI (procurement leader, business unit head, or C-level sponsor) has the most to gain from procurement transformation and has access to the Economic Buyer or Decision Maker? Request a 1-on-1 follow-up with that person to understand their priorities, political landscape, and how Zycus can help them win internally. Goal: convert a procurement leader or business unit head into an active champion who advocates for Zycus, introduces us to the EB/DM, and pushes back on competitors. Without a champion, Zycus will lose to whichever competitor has better internal advocacy.  _(by 2026-07-08; owner: Deal team)_
- Request vendor-selection timeline and approval chain from Franck or Yousef. In the 2026-06-23 demo or immediately after, ask: when will SAMI make the vendor-selection decision, who makes that decision (name and title), and what is the approval chain (procurement → business unit head → CFO → C-level?). If the decision timeline is beyond 2026-08-15, recommend slipping the close date to a realistic post-vendor-selection negotiation and contracting window (likely Q4 2026 or Q1 2027). If the decision authority is still procurement-level (Yousef or Franck), escalate to identify the real Decision Maker (likely VP-level or C-level).  _(by 2026-06-30; owner: Deal team)_

**Open commitments** (16)
- Provide exhaustive list of aerospace and defense OEMs using Zycus SRM with usage metrics  _(who: Zycus; open)_
- Provide next milestone or decision timeline after RFP clarifications and live demo 23 Jun  _(who: Buyer; open)_
- Deliver detailed data center location and InfoSec compliance documentation for Saudi defense-sector requirements  _(who: Zycus; open)_
- Clarify and confirm on-prem vs cloud deployment model acceptability to SAMI, and resolve data center location and InfoSec compliance documentation for Saudi defense-sector requirements  _(due 2026-07-01; who: Zycus; overdue)_
- Deliver exhaustive list of aerospace and defense OEMs using Zycus SRM with usage metrics  _(who: Zycus; overdue)_
- Resolve and confirm on-prem vs cloud deployment model acceptability to SAMI  _(who: Zycus; open)_
- Provide detailed data center location and InfoSec compliance documentation for Saudi defense-sector requirements  _(who: Zycus; open)_
- Provide data center location and InfoSec compliance documentation for Saudi defense-sector requirements  _(due 2026-06-27; who: Zycus; overdue)_
- Confirm on-prem vs cloud deployment model acceptability to SAMI (Franck requested clarification 21 May, Zycus responded, awaiting buyer confirmation)  _(who: Zycus; open)_
- Resolve data center location and InfoSec compliance documentation for Saudi defense-sector requirements  _(who: Zycus; open)_
- SAMI to provide vendor selection timeline and post-demo decision process  _(who: Buyer; open)_
- Provide next milestone or decision timeline after live SRM demo 23 Jun  _(who: Buyer; open)_
- Conduct live SRM demo and discuss RFP position, SIs, and data center with SAMI  _(due 2026-06-23; who: Zycus; open)_
- Provide written responses addressing defense-specific controls: FARS compliance, trade compliance, classification/lineage of supplier materials for classified projects, third-party validations, and consignment stock / S/4HANA MRP integration  _(due 2026-07-01; who: Zycus; open)_
- Confirm and document whether Zycus supports a historical price view (historical price book / PO-level price history) without relying on AI, and provide implementation approach and examples  _(due 2026-07-01; who: Zycus; open)_
- Provide details on recent S/4HANA integrations (including any Saudi implementations) and confirm iSaaS adapter capabilities and required API design inputs for SAMI's S/4 landscape  _(due 2026-07-01; who: Zycus; open)_

### SaskTel  -  SaskTel S2P Opp- Aug'25
_Owner: Mike Flowers  |  opp_id: 006P700000GmoPW_

**Critical moves** (6)
- Re-engage Jeffrey Buzila and Adriel Picard immediately to confirm RFP process status (did supplier presentations happen week of 20 Apr? what is current stage? where does Zycus rank? what is revised timeline to decision and contracting?). Frame as a check-in post-year-end-close window that Jeffrey deferred to (late Apr/early May, now 6-8 weeks past). Request a 30-minute call within 5 business days to align on next steps and revive momentum.  _(by 2026-07-01; owner: Deal team)_
- Request competitive debrief from Adriel Picard or Jeffrey Buzila during re-engagement call: where did Zycus rank on the RFP scorecard, what were evaluation strengths/gaps, who is the current frontrunner among the 4-vendor shortlist (Coupa, GEP, JAGGAER, SAP Ariba), and is the shortlist still active or has a finalist been selected. Use debrief to inform Merlin AI positioning and wedge strategy.  _(by 2026-07-01; owner: Deal team)_
- Multi-thread beyond Adriel and Jeffrey by engaging procurement managers (Lyndsey Pankratz Procurement Manager, Owen Winter Corporate Services Manager Supply Chain, Trisha M Procurement Manager) and Business Transformation lead (Bonnie Burnett) to build additional buyer-side relationships, identify champion candidates, and add them to opp contact roles. Request introductions from Adriel during re-engagement call.  _(by 2026-07-08; owner: Deal team)_
- Map and engage the Economic Buyer (CFO or finance sponsor who controls the $358k S2P budget). Donald Rober (CFO), Charlene Gavel (CFO), or Mike Anderson (CFO) are candidates on the account contact list but not engaged. Request introduction from Adriel or Jeffrey to the budget owner to validate financial approval, timeline, and strategic priority for this investment.  _(by 2026-07-08; owner: Deal team)_
- Validate AI as a decision criterion and position Merlin agentic AI as the differentiator. Surface with Adriel, Jeffrey, or CTO Daryl Godfrey / CIO John Hill whether AI-driven procurement automation was a weighted RFP criterion, how Zycus's Merlin scored vs competitors, and who the internal AI advocate is. Frame Merlin's agentic negotiation capability as the wedge if AI is a priority.  _(by 2026-07-08; owner: Deal team)_
- Surface and document the business pain and quantified value case with Adriel, Jeffrey, or the CFO/finance sponsor. Ask what operational/financial problem drove the RFP (manual-process cost, contract leakage, spend visibility gap, compliance risk, AI/automation gap), what the target ROI or savings is, and who the executive pain owner is (CFO, CTO, CIO). Quantify the value case to anchor urgency.  _(by 2026-07-22; owner: Deal team)_

**Open commitments** (2)
- SaskTel to confirm supplier presentation schedule or reschedule (originally week of 20 Apr 2026)  _(due 2026-04-20; who: Buyer; overdue)_
- Jeffrey Buzila to provide next steps post-year-end close (deferred to late Apr/early May 2026)  _(due 2026-05-09; who: Buyer; overdue)_

### Scheme Financial Vehicle  -  Scheme FInancial_CLM
_Owner: Luke Dougherty  |  opp_id: 006P700000QKfzN_

**Open commitments** (3)
- ERP integration questions response and integration session  _(who: Zycus; open)_
- Send ERP integration questions to Zycus (promised 'this week' as of 11 May)  _(due 2026-05-18; who: Buyer; overdue)_
- Provide feedback on 29 Apr scenario-based demo (expected 1 May)  _(due 2026-05-01; who: Buyer; overdue)_

**Implicit needs** (2)
- Share demo presentation slides and feature timelines for internal review  _(2026-04-29)_
- Send additional information on applications and offer to co-build niche applications  _(2026-04-29)_

### Sitecore  -  Sitecore_S2P_Apr26
_Owner: Steve Ovadje  |  opp_id: 006P700000WWRku_

**Open commitments** (3)
- Submit final RFP response by 30 Jun 2026 EOD  _(due 2026-06-30; who: Zycus; open)_
- Co-develop quantified ROI model for mid-Jul CFO business-case presentation (implicit commitment from 15 May call)  _(due 2026-07-15; who: Zycus; open)_
- Present business case to CFO and secure decision by end of Jul 2026  _(due 2026-07-31; who: Buyer; open)_

**Implicit needs** (2)
- Co-develop a quantified ROI model for the mid-Jul CFO business-case presentation, tying AP automation, Microsoft-native intake adoption, and Merlin AI capabilities to measurable outcomes (AP headcount hours saved, invoice-processing cycle time, error-rate reduction, spend-under-management increase)  _(2026-05-15)_
- Assign the same demo resource (Devika) used by the customer reference to maintain continuity and comfort for follow-up demos  _(2026-04-09)_

### SOUTH AFRICAN REVENUE SERVICES ( SARS )  -  SARS_eProcurement
_Owner: Caroline Lacocque  |  opp_id: 006P700000UZv8c_

**Open commitments** (1)
- Complete and submit RFI response to SARS  _(who: Zycus; open)_

### Swift SC  -  Swift Legal_CLM
_Owner: Caroline Lacocque  |  opp_id: 006P700000PONMr_

**Open commitments** (1)
- Confirm with the relevant stakeholder team whether Microsoft Graph API supports delta queries for real-time provisioning, and provide recommended sync approach (delta vs bulk) to Swift  _(due 2026-04-16; who: Zycus; overdue)_

**Implicit needs** (2)
- Proposal with migration cost split, work breakdown structure/Gantt timeline, stakeholder roles/effort slide pack, and workshop/design guidance documents  _(2026-06-15)_
- Swift to provide master contract counts and number of annexes to enable migration pricing and effort estimation  _(2026-06-15)_

### Swift SC  -  Swift_S2P BU 450
_Owner: Caroline Lacocque  |  opp_id: 006P700000XlSVh_

**Critical moves** (5)
- Executive connect: the deal owner's manager engages Swift's Chief Procurement Officer (Alessia Ferrari or Dominique Kelder) directly to unblock the signed amendment (awaited 23 days) and PO (requested 16 days ago) by 26 Jun, framing it as a low-complexity 20K EUR expansion of an active S2P relationship that should not require protracted procurement review, and securing a commitment to deliver both by end-of-week to preserve the 30 Jun close and protect the broader Swift partnership.  _(by 2026-06-26; owner: Executive connect)_
- Product escalation: convene internal Zycus iSaaS/SSO product and integration architecture review (include Patrick, Amar, and the iSaaS product owner) by 26 Jun to determine whether the proposed CPI-Graph API architecture can support delta queries and achieve near-real-time (sub-5-minute) Azure AD group-membership sync as Jairo required on 18 Jun, or if a standards-based solution (SCIM provisioning, claims-based role sync) is required; then present a written technical proposal with sync-latency SLA and roadmap (if standards are needed) to Swift's security and audit stakeholders by 27 Jun to unblock integration development and preserve the IAD timeline.  _(by 2026-06-27; owner: Product escalation)_
- Deal team: the deal owner (Caroline Lacocque) identifies and directly engages the internal Swift sponsor who approved the 20K EUR amendment, confirms their active commitment to delivering the signed contract and PO by 26 Jun, and adds them as Champion and Economic Buyer Contact Roles on the opportunity by 27 Jun, closing the zero-buyer-Contact-Role gap and creating a named escalation path if the CPO-level executive push (move 1) does not yield the signed paper.  _(by 2026-06-27; owner: Deal team)_
- Deal team: if signed amendment and PO are not received by 28 Jun, the deal owner updates the close date to a realistic 10-14 Jul window (reflecting 10-14 day slip from today) and moves forecast to Best Case, then communicates the revised timeline and the cause (buyer procurement delay, not commercial objection) to the deal owner's manager and to the relevant stakeholder sponsor (identified in move 3) by 1 Jul, protecting forecast accuracy and relationship trust.  _(by 2026-07-01; owner: Deal team)_
- Deal team: once signed amendment and PO are received, schedule a post-signature alignment call with the Swift sponsor (identified in move 3), the Core Leadership team (touchpoint 18 Jun), and key IAD workshop stakeholders by 8 Jul to re-anchor the business case for the Extra BU expansion (headcount, category scope, compliance driver), confirm the IAD roadmap and go-live timeline, and surface the Azure AD integration resolution (from move 2), framing the call as the formal implementation kick-off and ensuring the buyer sees Zycus as a strategic partner, not a transactional vendor.  _(by 2026-07-08; owner: Deal team)_

**Open commitments** (5)
- Return signed copy of amendment (shared 1 Jun 2026)  _(due 2026-06-24; who: Buyer; overdue)_
- Issue PO (requested from Olie 8 Jun 2026)  _(due 2026-06-24; who: Buyer; overdue)_
- Hold internal Swift stakeholder meeting (security, audit, Patrick, Amar) to decide acceptable Azure AD sync approach (delta vs bulk, async vs real-time) and revert with decision to Zycus  _(due 2026-06-27; who: Buyer; open)_
- Confirm with the buyer's technical team whether Microsoft Graph API supports delta queries for group-membership sync to enable near-real-time provisioning  _(due 2026-06-27; who: Zycus; open)_
- Organize internal Zycus technical meeting to decide acceptable synchronization approach (delta vs bulk) and provide proposal to Swift  _(due 2026-06-27; who: Zycus; open)_

### Techtronic Industries Company Limited  -  Techtronic Industries S2P
_Owner: Luke Dougherty  |  opp_id: 006P700000GWfrf_

**Open commitments** (1)
- Make vendor decision and award contract  _(due 2026-05-29; who: Buyer; overdue)_

### Trillium Foods  -  Trillium Foods Opp Jan '26
_Owner: Bailey Erazo  |  opp_id: 006P700000SyAE5_

**Critical moves** (5)
- Re-engage Ron by end of day 25 Jun (tomorrow) via phone and email: confirm internal pricing approval status, ask directly 'what would it take to choose Zycus this week, and what concerns are you working through with your team?', listen for budget/EB/competitor blockers, then co-author a mutual close plan with a realistic revised close date (likely mid-to-late Jul) and the specific next steps (EB meeting, final approval, contracting). Convert 5 days of silence into forward momentum and a committed timeline.  _(by 2026-06-25; owner: Deal team)_
- Map and engage the Economic Buyer by 28 Jun (within 4 days): in the re-engagement call with Ron (rank-1 move), ask 'who approves the $160k budget on your side, and what is their timeline and top concern?', then offer to package a one-page quantified ROI summary (savings model tied to error reduction, manual-work elimination, and sourcing opportunity from unified spend visibility across three plants) that Ron can present upward to his CFO/VP/ownership. If Ron is hesitant, position it as 'we've helped other procurement leaders build the CFO-ready business case — let us do the same for you so you have a strong story to take forward.' Secure Ron's introduction to the EB or commit to delivering the ROI package by 28 Jun for Ron to present internally by end of week.  _(by 2026-06-28; owner: Deal team)_
- Decode the competitive position by 28 Jun (within 4 days): in the re-engagement call with Ron (rank-1 move), ask directly 'how do the two finalists compare in your evaluation, and what stands out about Zycus?' Listen for price vs capability tradeoffs, then anchor the decision on Ron's prior Zycus success at Tate & Lyle: 'You know the platform works from your own experience, you've seen the AI advancements we've made since then (AutoClass, 95% OCR, Merlin NLP), and we can de-risk your rollout with that proven track record — what would it take to move forward with Zycus this month?' If price is the blocker, explore creative commercial structures (phase the implementation across two contract years to spread cost, reduce Year 1 scope to core spend analytics + supplier harmonization and defer advanced features, or offer a 90-day pilot) rather than blanket discounting, preserving deal value while removing the price objection.  _(by 2026-06-28; owner: Deal team)_
- If no reply from Ron by end of day 26 Jun (day of stated close), escalate to executive connect (Michael McCarthy, deal owner's manager) by 30 Jun: McCarthy reaches out senior-to-senior to Ron's superior (VP Supply Chain, CFO, or family ownership, inferred from Ron's need for business case and Trillium's family-run structure) with a message of 'We've been working closely with Ron on a unified spend analytics solution to support your three-plant consolidation; I wanted to check in at the leadership level to ensure this aligns with your priorities and timeline, and to offer our support in building the business case if that would be helpful.' Goal: validate executive-level interest and urgency, map the EB, and unlock a stalled buyer thread with senior engagement.  _(by 2026-06-30; owner: Executive connect)_
- Prepare and send a quantified one-page ROI summary by 2 Jul (within 8 days): build a savings model for Trillium anchored to (1) time savings from eliminating manual Excel consolidation and classification (estimate hours/week saved × Ron's loaded cost + procurement team time), (2) error reduction and data quality improvement (% reduction in classification errors × cost of rework/bad decisions), and (3) sourcing opportunity from unified spend visibility (baseline: 3–5% savings on addressable indirect + packaging spend, applied to Trillium's estimated annual spend across three plants). Tie the $160k investment to 12-month payback or better. Use Zycus case studies (e.g., OCR accuracy improvement 80% → 95%, AutoClass classification accuracy 95–100%) and Ron's own articulated pain ('running blind', manual errors, no seasonality visibility) as proof points. Deliver to Ron as a leave-behind he can present to his CFO/EB, positioning Zycus as the partner that equips him to win internal approval.  _(by 2026-07-02; owner: Deal team)_

**Open commitments** (1)
- Internal approval of split implementation pricing (Ron acknowledged receipt 19 Jun, is 'reviewing with team')  _(who: Buyer; open)_

### Ubisoft Entertainment  -  Ubisoft_S2P
_Owner: Pierre Meraud  |  opp_id: 006P700000TJa8k_

**Open commitments** (1)
- Ubisoft feedback milestone (post-demo evaluation, vendor selection input)  _(due 2026-07-31; who: Buyer; open)_

### Wheelson Technologies  -  Mumtalakat (Bahrain Sov Wealth Fund) - lead by Wheelson Tech
_Owner: Dan Quinn  |  opp_id: 006P700000VlPdp_

**Open commitments** (4)
- Mumtalakat (via Wheelson) to confirm resumption timeline and next steps for Bahrain workshop after regional geopolitical delay  _(who: Buyer; overdue)_
- Wheelson to finalize date and agenda for 1-2 day Bahrain workshop with Mumtalakat stakeholders (anticipated next step per Phase 2 plan)  _(who: Buyer; open)_
- Execute NDA for deeper evaluation phase (required before workshop per plan)  _(who: Buyer; open)_
- Deliver deeper NDA-based workshop and be on standby for paid POC if Zycus remains in final evaluation (conditionally committed per Phase 2 plan)  _(who: Zycus; open)_
