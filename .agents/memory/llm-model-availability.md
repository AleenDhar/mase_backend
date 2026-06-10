---
name: LLM model availability in this env
description: Which LLM providers respond when invoking models, and the OpenAI-with-many-tools hang that decides model choice for tool-using agents.
---

# LLM model availability

Both providers respond on actual invocation in this environment now:
- Anthropic works (`api.anthropic.com/v1/messages` returns 200). The app default
  `anthropic:claude-sonnet-4-5` and the webhook opportunity-analyzer run fine on it.
- OpenAI works for plain chat (`openai:gpt-4o`, `openai:gpt-4o-mini`).

(An earlier note claimed Anthropic 404s here; that is no longer true — verified by
live 200s from the analyzer and the deal-engine sweep.)

## The decisive gotcha: OpenAI hangs when many MCP tool schemas are bound

`openai:gpt-4o` HANGS at the very first model call when a large MCP tool catalog
(~27 Salesforce+Avoma tools) is bound to the agent — no error, no timeout firing,
just an infinite hang before any HTTP call goes out. The SAME model is fine with NO
tools (the deal-engine `/chat` strategist uses `gpt-4o` and answers in seconds).

**Why:** large/strict tool-schema payloads to OpenAI stall in this env; Anthropic
handles the identical toolset without issue.

**How to apply:** for any TOOL-USING agent here (sweep, analyzer, anything binding
the MCP catalog), default to Anthropic claude-sonnet, not OpenAI. Make the default
tool-safe: resolve an explicit feature override, else a known-good Anthropic pin —
do NOT inherit a generic `MODEL` env that a deployment might set to OpenAI, or the
hang silently returns. OpenAI is fine for no-tool chat/completions.
