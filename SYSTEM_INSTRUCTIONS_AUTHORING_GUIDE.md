# System Instructions Authoring Guide

**Purpose.** Hand this doc to Claude (or any LLM) along with the current
`ABM Prospecting Engine — System Instructions` and a change request.
Claude will use these rules to produce the updated system instructions
in the right format, tone, and structure — without breaking the
contract the agent and the surrounding infrastructure already depend on.

**Use it like this:**

> "Here is `SYSTEM_INSTRUCTIONS_AUTHORING_GUIDE.md`. Here is the current
> `v11.X` system instructions. Here is what I want changed: [list].
> Produce the updated full system instructions following the guide."

---

## 0. Read this first

The system instructions are the contract between the user and the
ABM agent. They are also part of a larger system:

- A **verifier** runs after every agent turn and mechanically checks
  that required tool calls fired (see `verifier/flows/abm_v11.py`).
- A **remediation loop** re-prompts the agent ONCE if checks were
  missed (see `verifier/remediation.py`).
- The **frontend** renders certain message types specially
  (`verifier_report`, `verifier_remediation`).

When you change the system instructions, you may also need to change
the verifier flow spec. **Call this out explicitly** at the end of the
output so the human knows.

---

## 1. The Two-Layer Model

Every rule in the system instructions belongs to ONE of two layers.
Decide which layer before writing.

### Layer A — Verifier-enforced (mechanical, tool-coverage)

These are checks that look at the tool-call log: "did `web_search` fire
with a CPO query?", "did all four RAG files load?", "did Lemlist push
happen?".

**Rules in this layer:**
- Should appear ONCE in the system prompt, in the relevant persona
  section. Don't repeat them in §0.x summary tables and quality-floor
  checklists too.
- Should be phrased as design intent, not a "MUST" gate.
  Example: "The Researcher fires 7 search angles in parallel
  (CPO / Head of Procurement / Head of Sourcing / CFO / Procurement
  Leadership / LinkedIn procurement / LinkedIn finance)."
- The corresponding `ExpectedCall` rule must exist in
  `verifier/flows/abm_v11.py`. If it doesn't, flag it.

### Layer B — Agent-owned (judgment, content, voice)

These are things the verifier can't see: anti-archetype scans, em-dash
detection, Rule 10 (no competitor critique), Rule 3 (proof traceability),
voice mode, intel source tagging, yields, anti-repetition matrix,
frame discipline.

**Rules in this layer:**
- Should stay verbose, prescriptive, and explicit.
- Promote them to a clear "Quality Standards" or "Content Gates"
  section so it's obvious the agent — not the verifier — owns them.
- These are where the system prompt earns its weight.

**Heuristic:** if you can write a function `check(tool_calls) -> bool`
in under 20 lines, it's Layer A. If you need to read the agent's prose
output to judge it, it's Layer B.

---

## 2. Non-Negotiable Sections

Every version of the system instructions MUST contain these, in this
order. Do not remove, rename, or reorder them without explicit user
approval.

1. **Title block** — name + version + tag line. Example:
   `ABM Prospecting Engine — System Instructions v11.2`
2. **Version-changes block** — bulleted list of what changed vs the
   previous version, dated. New versions ALWAYS get one.
3. **Section 0 — Foundation** — what the engine does, the critical
   rules list, intake gate, tools, execution model, parallelization map.
4. **The 10 Critical Rules** — numbered list. See §3 below for how to
   evolve them.
5. **Per-persona sections** (Researcher / Mapper / Strategist / Writer /
   Operator) in execution order.
6. **Coverage check & remediation** section — see §6 below. NEW in v11.2+.
7. **Realistic run times** table.
8. **Anti-Pattern Library** version reference.

---

## 3. The Critical Rules List

The "10 Critical Rules" is the single most-read part of the document.
Treat it as a contract.

**Rules of the rules list:**

- **Number is identity.** Rule 9 is "contactOwner = usr_ ID at root
  level." Forever. Do not renumber. If a rule is removed, mark it
  `~~RESERVED~~` and explain in the version-changes block.
- **Each rule has a Scope column.** Phases the rule applies to.
  Mandatory.
- **Rules can have parts.** Rule 10 has Part A and Part B. This is
  allowed — keeps the count stable when scope expands.
- **A rule should be one mechanical assertion.** "Do X. Don't do Y."
  Not a paragraph of philosophy. Philosophy lives in the persona
  sections.
- **If a rule is now Layer A (verifier-enforced), keep it in the list
  but soften the phrasing** ("...the verifier will re-prompt if missed")
  so the agent knows there's a safety net.

**When adding a new rule:** append at the end (Rule 11, 12...) — never
insert. Do not promote to "critical" unless it is genuinely new
operational discipline.

---

## 4. Style Conventions

### Voice
- Second person, imperative. "You are a senior ABM strategist..."
- Direct. No hedging. No "please" or "kindly."
- Persona descriptions in third person within the architecture diagram
  ("Researcher owns account intelligence"); prescriptive content in
  second person.

### Formatting
- Top-level sections use `SECTION N — TITLE` (all caps, em dash).
- Sub-sections use `N.M Heading` (numbered).
- Tables for any "if X then Y" mapping.
- Bullets for parallel concerns. Numbered lists for sequential steps.
- Code blocks for tool names, IDs, payloads, exact wording.

### Em dashes (meta-rule)
The system instructions are a *prompt*. Em dashes in the prompt are
fine. Em dashes in the agent's *output* are forbidden (Rule 11 in
v11.1's Anti-Pattern Library). Do not confuse the two.

### Length
- Aim for tight. v11.1 is ~1270 lines and that's near the upper bound.
- Every "MUST" should justify its tokens. If a rule is restated in 3
  places, delete 2.
- Persona sections can be longer (they carry craft); §0 should be
  tight.

---

## 5. Versioning Rules

Use semantic-ish versioning: `v<MAJOR>.<MINOR>`.

- **Minor (v11.1 → v11.2):** rule wording tweaks, new anti-patterns,
  RAG file version bumps, additions to the parallelization map. No
  contract changes.
- **Major (v11 → v12):** persona architecture change, yield-point
  change, removal/renumbering of critical rules, change in input
  contract.

**Always update the version-changes block.** Format:

```
v11.2 changes vs v11.1 (Month YYYY, [reason]):
- [Section reference] — what changed and why.
- ...
```

Date is required. Reason should reference an actual event ("post-Acme
test run", "after verifier rollout", "user feedback on draft tone")
when possible.

---

## 6. The Coverage Check & Remediation Section

This section MUST exist in v11.2+ and explains how the agent should
react when the verifier re-prompts. Suggested wording:

> **Coverage check & remediation**
>
> After every run, an external verifier checks that all required tool
> calls fired. If gaps are found, you will receive ONE follow-up
> message starting with `🔍 Coverage check — verifier follow-up`,
> listing what's missing, grouped by phase.
>
> When you see this message:
> 1. Read the list. It's already deduplicated and only contains
>    actually-required gaps (advisory items are not surfaced).
> 2. Run only the missing calls. **Do not redo work that already
>    succeeded.**
> 3. If the new data changes a draft, decision, or routing call,
>    update the affected output. If it doesn't, say so explicitly.
> 4. Produce a short final answer noting what was added.
>
> You get exactly ONE remediation turn — the verifier will not prompt
> again after that. Treat it as a checklist, not a conversation.
>
> **Two corollaries:**
> - **When in doubt, fire the tool.** Remediation costs latency and
>   tokens. It's cheaper to over-call slightly in the first pass than
>   to be re-prompted.
> - **Don't claim you ran a tool you didn't.** The verifier reads the
>   actual tool-call log, not your prose. Saying "I searched LinkedIn
>   for the CFO" without firing `web_search` will trigger remediation.

---

## 7. Forbidden Patterns

Never write the system instructions in any of these ways:

1. **Don't apologize on behalf of the agent.** No "if this is
   confusing, please clarify." The agent should decide and log.
2. **Don't include implementation hints for the verifier or the
   server.** ("The verifier checks for substring 'CPO'...") If the
   matching logic changes, the prompt becomes a lie.
3. **Don't reference file paths in the codebase** (`server.py`,
   `verifier/flows/...`). The agent doesn't have access to them.
   File references belong in commit messages and `replit.md`, not
   in the prompt.
4. **Don't include user names, account names, or specific contact data.**
   Examples in the prompt should be either obviously fake (`Acme Corp`)
   or use the placeholder `<ACCOUNT>`. Never paste a real test run.
5. **Don't include keys, tokens, or environment variable names.**
6. **Don't put process meta-commentary inside the prompt** ("This rule
   was added because in May 2026 we noticed..."). That belongs in the
   version-changes block at the top.
7. **Don't use emojis except in the Coverage Check section** (where
   `🔍` and `⚠️` / `✅` carry semantic meaning the agent must recognize).

---

## 8. Output Contract

When Claude produces an updated system instructions doc using this
guide, the output MUST:

1. Be a single fenced markdown block (or plain text if requested),
   ready to paste verbatim into the system-prompt field.
2. Start with the title block and version-changes block.
3. Preserve the rule numbers from the previous version (see §3).
4. End with a short **post-output note** (outside the prompt body, in
   the chat reply) that includes:
   - **Summary of changes** — bullets, ≤8 lines.
   - **Verifier impact** — does `verifier/flows/abm_v11.py` need a
     change? List the specific `ExpectedCall` IDs to add/remove/edit,
     or say "none."
   - **Test prompts** — 1–3 prompts the user can paste into the UI to
     verify the changes work end-to-end.

If the requested change conflicts with anything in this guide
(e.g., the user asks to remove the Coverage Check section, or to
renumber rules), **flag the conflict and ask before doing it.** Do
not silently override the contract.

---

## 9. Quick Reference — Where Stuff Lives

| If the change is about... | Edit this |
|---|---|
| Which tools are required to fire | `verifier/flows/abm_v11.py` AND the relevant persona section |
| Tool-call argument patterns (e.g., "CPO" must appear in the query) | `verifier/flows/abm_v11.py` (matchers) |
| What "good output" looks like (voice, em dashes, frame matrix) | System prompt, Quality Standards section |
| Yields (when to stop and ask) | System prompt, Execution Model |
| Intake required inputs | System prompt §0.2 + frontend validation |
| New project added to the flow | `verifier/flows/abm_v11.py` `ABM_PROJECT_IDS` tuple |
| Remediation behavior (cap, prompt format) | `verifier/remediation.py` (NOT the system prompt) |
| Realistic run times | System prompt, Run Times section |
| Anti-pattern catalog | System prompt, Anti-Pattern Library |

---

## 10. Final Checklist (run this before shipping a new version)

- [ ] Version number bumped, version-changes block updated with date
      and reason.
- [ ] All 10 critical rules present, numbered correctly, no
      renumbering.
- [ ] Coverage Check & Remediation section present and unmodified
      (unless user explicitly approved a change).
- [ ] No file paths, no real customer names, no secrets.
- [ ] Layer A rules appear once (per §1). Search the doc for
      duplication of any tool name and trim.
- [ ] Layer B rules are explicit and prescriptive.
- [ ] Anti-Pattern Library version reference matches reality.
- [ ] Post-output note lists verifier-flow-spec changes (or "none")
      and test prompts.
