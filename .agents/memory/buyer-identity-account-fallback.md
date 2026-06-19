---
name: Buyer-identity account-contacts fallback
description: When an opp is thin on contact roles (<3), recover stakeholders/domains/sibling-opps via DIRECT Contact/Opportunity-WHERE-AccountId queries (never the Account.Contacts child subquery, which the gateway always returns empty), then scope-route shared-account calls by subject — NOT by Avoma's opp association.
---

# Buyer-identity account-contacts fallback

`_buyer_identity()` in `deal_engine_sweep.py` prefetches, via direct SOQL
(`_soql` → `sf_conn().query_all`, bypassing the agent's summarised MCP path), the
buyer-side identity used to drive Avoma account+attendee discovery and the
dark-vs-discovery-miss decision.

**Primary path (unchanged):** `OpportunityContactRole WHERE OpportunityId = …` —
the contacts actually attached to the opp.

**Fallback (added):** when the opp is THIN on contact roles (`roles_count < 3`,
the multi-thread bar — validated 2026-06-19 on the VP forecasted book), resolve
the opp's `AccountId` (now selected in the head query) and pull:
- `Contact WHERE AccountId = '<acct>' AND Email != null ORDER BY LastModifiedDate
  DESC LIMIT 50` → `account_contacts` (domains fold into the `domains` set).
- sibling OPEN opps: `Opportunity WHERE AccountId = '<acct>' AND Id != '<self>'
  AND IsClosed = false` → `sibling_opps`.

Also sets `contact_roles_thin` and carries `self_name` (opp name = scope anchor).

**Scope routing (the important rule):** an account often runs several distinct-
scope deals at once (Swift: CLM vs S2P/BU-450 vs S2P-renewal vs Certinal e-sign).
A shared-account call/stakeholder belongs to THIS opp ONLY if its call SUBJECT
matches this opp's scope. Do NOT route by shared domain, and do NOT trust Avoma's
own opp association — it mis-attributes across opps and even across accounts
(validated 2026-06-19: Swift's 133 `swift.com` calls were nearly all stamped onto
the S2P opp `006P700000XlSVhIAN`, and the CLM-scope "iContract" calls onto a
*different account* `0010O00002EPu0JQAT`). The agent block injects `sibling_opps`
+ the scope-anchor + an explicit "route by subject, not Avoma association" rule.

**Partner exception:** a contact role can be a partner / SI / reseller (e.g. ROJO
on Swift), not a buyer employee. NEVER drop them — they are real stakeholders and
often the channel the deal runs through. Each OCR role is enriched with the
contact's own `company` (`Contact.Account.Name`) and `domain`, and tagged
`is_partner`. Partners are rendered separately in the block and retained in full.
The "thin / single-threaded" test is based on `buyer_roles_count` (partners
excluded) so a 1-buyer + 2-partner opp still triggers recovery instead of reading
as well-threaded.

Classification does **NOT** use the website domain alone — a buyer employee can sit
on a corporate ALIAS / subsidiary domain that differs from the website (validated
2026-06-19: Fortive website `fortive.com`, but employees on `ftvbsllc.com` — Alex
Becker was being mis-flagged as a partner). Instead we build a **buyer-domain SET**
= website domain + every domain that CLUSTERS across the account's own contacts
(`Contact WHERE AccountId`, a domain on ≥2 contacts); a role is a partner only if
its domain is OUTSIDE that set. The extra account-domain query is paid **only when
a role is off the website domain** (the common all-on-website case stays free).
Guardrails: a one-off mis-parented contact (domain on <2 account contacts) won't
widen the set; if no buyer-domain set can be built at all, every role is treated as
buyer (never silently de-weight a real contact).

**To-do nudge:** when `contact_roles_thin` (buyer-side < 3), the block instructs the
agent to emit an "add the missing buyer-side contact roles" action item (data
hygiene + single-threading risk), framed as an action, not housekeeping.

**Why:** a multi-threaded account read as single-threaded/dark. Root cause: the
DeepAgent Salesforce gateway **never materialises child-relationship subqueries**
— `(SELECT … FROM Contacts)` (i.e. `Account.Contacts`) returns `[0 records]`
even when the contacts exist (confirmed 2026-06-19 on Omnia Holdings Limited,
acct `0012000000mblavAAA`: 25 `@omnia.co.za` contacts present, child subquery
read 0). The opp itself had 23 contact roles, so the *primary* path covers most
deals; the fallback is for genuinely empty-contact-role opps where the account
still has the people (and the mailbox domain we need for Avoma matching).

**How to apply / invariants:**
- ALWAYS query the child object directly by FK; never a parent child-subquery on
  the gateway path (`Account.Contacts`, `Account.Opportunities`, etc. → always 0).
- `account_contacts` are ACCOUNT-level, NOT confirmed opp stakeholders. The agent
  block (`_buyer_identity_block`) labels them as domain/mailbox + multi-thread
  candidates only — do not promote them into the opp stakeholder map as facts.
- Fallback fires only when `roles_count == 0` (don't double-pull when the opp has
  its own roles).
- Read-only; SF write lockdown still applies. See `salesforce-write-lockdown.md`.
