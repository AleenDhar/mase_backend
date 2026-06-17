# CLAUDE.md

Read **[AGENTS.md](AGENTS.md)** — it is the operating guide for any coding agent in this
repo (start-of-session reading order, standing conventions, and copy-paste prompts).

Quick reminders:
- **Read `CHANGELOG.md` first every session** (and after every `git pull`). Append an
  entry when you change behaviour.
- **Agent system prompts live in Supabase**, not in `prompts/*.md` (those are deprecated
  seeds). Edit prompts in Admin → Agent Control. See
  `.agents/memory/prompts-source-of-truth.md`.
- **Never write to Salesforce** (`MCP_TOOL_DENYLIST`).
- **`deploy.ps1` ships the working tree** (not GitHub); deploy intentionally.
- Architecture + conventions: [replit.md](replit.md). Durable rules: `.agents/memory/`.
