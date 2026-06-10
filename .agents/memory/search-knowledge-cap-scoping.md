---
name: search_knowledge cap scoping
description: Why the RAG search_knowledge cap is per model-step, not per whole run
---

The `search_knowledge` per-turn cap must be scoped **per model step**, not per whole agent run.

**Why:** An agent run auto-continues across many model steps (loops on `max_tokens`).
A per-run cap accumulates every step's RAG searches into one budget, so a legitimately
RAG-heavy multi-step workflow (e.g. an ABM pipeline phase) blows past the cap with
*distinct, needed* queries — not a true loop. The escalation then hard-cancels the
whole run, losing all output and burning the spend (seen in prod: a cancelled turn
cost ~$4 / 2.1M input tokens with no final answer; surfaces as
missing_terminal / empty_phase_output / "Run failed — Phase 1 (error)").

**How to apply:**
- Keep the cap counter step-scoped (reset at the top of each auto-continue iteration).
- Keep the dedupe memory (seen queries) **run-scoped** so the SAME query is still
  blocked across steps.
- Let the hard-cancel escalation fire only when a SINGLE step spams (cap + N hits) —
  that's the original anti-abuse case (one step doing 9+ searches).
- Run-level runaway protection stays with the cost/time/continuation circuit breakers,
  NOT the RAG cap.
- The WS `/ws/chat` path is a single non-auto-continuing astream, so per-run == per-step
  there; it needs no step reset.
