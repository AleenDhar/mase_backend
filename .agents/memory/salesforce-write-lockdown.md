---
name: Salesforce write lockdown
description: Hard rule — the system must never write to Salesforce; enforced via an MCP tool denylist.
---

# Salesforce write lockdown

The system must **NEVER** create/update/delete anything in Salesforce. This is a
standing, emphatic user rule.

Enforced by the `MCP_TOOL_DENYLIST` env var (fnmatch globs against bare tool names),
set to: `create_record, update_record, delete_record, create_contact,
update_contact_email_from_ai, set_hot_abm_status, create_task`.

**Why:** these are the Salesforce-mutating MCP tools. The denylist is applied to the
*shared* tool catalog at MCP load, so it covers BOTH the main chat agent and the
analysis AI cells (which pull the same catalog via `analysis_engine.set_tool_provider`
/ `AgentManager.get_all_tools`). Without it, an unattended analysis Run-All could
autonomously write to Salesforce across many cells.

**How to apply:** never remove these denylist entries; if adding new Salesforce
write-capable tools, add them to the denylist too. Salesforce READ tools (`soql`,
`get_record`, `describe_object`, `list_objects`, `search`) are intentionally kept.
Verify after any change by checking the startup `[TOOL-FILTER] MCP_TOOL_DENYLIST=…`
log line and that the write tool names are absent from the agent's tool list.
