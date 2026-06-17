# AGENTS.md — operating guide for coding agents (`mase_backend`)

You are an AI coding agent working in the MASE backend. This file is your standing
brief. Read it at the **start of every session**, then follow it. (Claude Code, Cursor,
Codex, Aider and similar tools load this file automatically.)

MASE is an enterprise B2B revops platform (FastAPI on AWS ECS Fargate + Supabase + 17
MCP servers + LangGraph agents). It is past prototype — target ~1000 concurrent users —
so correctness, safety, and leaving a clear trail matter.

---

## 1. START HERE every session (in this order)

1. **`CHANGELOG.md`** — read the newest entries. This is the running log of
   behaviour-changing decisions. It tells you what changed recently and how to work
   with it. **After a `git pull`, re-read the top of this file** (the post-merge hook
   prints what changed).
2. **`.agents/memory/MEMORY.md`** — the index of durable rules; open any note relevant
   to your task.
3. **`replit.md`** — architecture overview + the **Conventions** section.

If you only read one thing, read `CHANGELOG.md`.

## 2. Standing conventions (do not violate)

- **Agent system prompts live in SUPABASE, not in code.** Rows in
  `public.jarvis_settings` (`mase_deal_sweep`, `mase_todo_runner`, `mase_chat_agent`),
  read via `agent_prompt_store.get_prompt(<id>)`, edited from Admin → Agent Control.
  Supabase is the SOURCE OF TRUTH. The `prompts/*.md` files are DEPRECATED cold-start
  seeds — **do not edit them to change behaviour.** See
  `.agents/memory/prompts-source-of-truth.md`.
- **Never write to Salesforce.** Enforced by `MCP_TOOL_DENYLIST`. See
  `.agents/memory/salesforce-write-lockdown.md`.
- **Deploys ship the WORKING TREE, not GitHub.** `deploy.ps1` builds the local tree via
  CodeBuild → ECR → blue/green ECS. So: commit AND deploy intentionally; a `git push`
  alone changes nothing in production, and a deploy ships whatever is on disk.
- **Never pass inline JSON to the AWS CLI from PowerShell** (it strips quotes and has
  corrupted Secrets Manager values). Set env via the `deploy.ps1` task-def, or write
  JSON to a file and pass `file://`.
- `.env*` stays gitignored; never commit secrets.
- Anthropic limits on this account: ITPM 2,000,000 / OTPM 400,000 / RPM 20,000 — design
  concurrent LLM work against these.

## 3. When you change something (leave a trail)

1. **Append a `CHANGELOG.md` entry** for any behaviour change, new endpoint, or contract
   change: `## YYYY-MM-DD — <title>` then **What / Why / How to work with it**.
2. For a **durable rule**, add a `.agents/memory/<slug>.md` note (one fact, with
   `name`/`description` frontmatter + Why/How) and a line in `.agents/memory/MEMORY.md`.
3. **Verify before commit:** `python -m py_compile <changed>.py`. Keep changes scoped.
4. Commit with a clear message; deploy only when you intend to ship.

## 4. Copy-paste prompts

Hand these to your agent verbatim.

**▶ Session start / after `git pull` (catch-up):**
```
Read AGENTS.md, then CHANGELOG.md (the newest entries) and .agents/memory/MEMORY.md.
Summarise what changed recently and which standing conventions apply, then tell me how
they affect this task: <describe task>. Flag anything in my task that conflicts with a
convention (e.g. editing a deprecated prompts/*.md file instead of the Supabase prompt).
```

**▶ I just pulled — what changed?:**
```
Run `git log --oneline @{1}..HEAD` (or since my last work) and show the CHANGELOG.md
diff for that range. Summarise the behaviour/contract changes and whether any touch the
files I'm about to work on.
```

**▶ Before you commit (wrap-up):**
```
Before committing: (1) append a CHANGELOG.md entry (What/Why/How) for any behaviour or
contract change; (2) if you established a durable rule, add a .agents/memory note + a
MEMORY.md line; (3) run py_compile on changed Python; (4) confirm you did NOT edit a
deprecated prompts/*.md to change behaviour (edit the Supabase prompt instead). Then
write a clear commit message. Do not deploy unless I asked.
```
