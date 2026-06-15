# WordPress MCP server

`wordpress_mcp_server.py` exposes one WordPress site to the agent through the
WordPress core REST API (`/wp-json/wp/v2`). It is a stdio MCP subprocess
declared in `mcp_config.json`, like every other server.

## Auth model — Application Password (no SSH)
Authentication is HTTP Basic over HTTPS using a WordPress **Application
Password** (built into WP 5.6+). Credentials come from env (injected via
`mcp_config.json`):
- `WORDPRESS_URL` — site base URL, e.g. `https://staging.example.com`
- `WORDPRESS_USERNAME` — a dedicated, least-privilege WordPress user
- `WORDPRESS_APP_PASSWORD` — an Application Password for that user (whitespace
  is stripped before use, so it can be pasted with or without the display spaces)

Application Passwords are per-user, independently revocable, and never expose the
account's real login password. Nothing is hardcoded. If any credential is unset,
every tool returns a structured `{"error", "missing", "detail"}` instead of
failing silently — so the server still loads even before secrets are set; calls
just error until they are.

## Security posture
- **Staging-first.** This grants full admin-level content/site control over
  whatever site the credentials point at. Point it at a staging site unless
  production management is explicitly intended.
- **Not a code-deploy channel.** Scope is limited to what the core `wp/v2` REST
  API exposes (content, taxonomies, comments, media, users, plugin lifecycle,
  core settings). It cannot deploy theme/plugin PHP, run WP-CLI, or touch the
  server filesystem — that still needs SSH/SFTP/Git.
- **Least privilege.** Capabilities of the configured WP user gate what
  actually succeeds; `wordpress_whoami` reports the live capability set.
- The `wordpress_*` tool prefix keeps these tools clear of the Salesforce
  `MCP_TOOL_DENYLIST` (which targets bare SF write-tool names).

## Tools (42, all `wordpress_` prefixed)
- **Discovery:** `whoami` (current user + capabilities), `list_routes` (site
  namespaces/routes + auth info).
- **Posts:** `list_posts`, `get_post`, `create_post`, `update_post`,
  `delete_post`. Create/update default new content to `status="draft"`.
- **Pages:** `list_pages`, `get_page`, `create_page`, `update_page`,
  `delete_page`.
- **Taxonomies:** `list_categories`/`create_category`/`update_category`/
  `delete_category` and the matching `*_tag` tools.
- **Comments:** `list_comments`, `get_comment`, `moderate_comment`
  (approve/hold/spam/trash), `delete_comment`.
- **Media:** `list_media`, `get_media`, `upload_media_from_url`,
  `upload_media_from_base64`, `delete_media`.
- **Users:** `list_users`, `get_user`, `create_user`, `update_user`,
  `delete_user` (permanent; `reassign` to hand off content).
- **Plugins:** `list_plugins`, `get_plugin`, `activate_plugin`,
  `deactivate_plugin`, `install_plugin` (from a WordPress.org slug),
  `delete_plugin` (must be inactive first).
- **Settings:** `get_settings`, `update_settings` (title, tagline, timezone,
  date/time formats, posts-per-page, front-page, default category, …).

## Conventions
- Reads use `context=edit` so drafts/private items and edit-only fields are
  visible to the admin agent.
- List tools wrap results as `{total, total_pages, count, items}` (totals read
  from the `X-WP-Total*` response headers).
- Deletes that support Trash (posts/pages/comments) trash by default; pass
  `force=True` to delete permanently. Terms, media, and users are always
  permanent (WordPress has no Trash for them).
- Errors are returned as `{"error", "status", "detail"}` carrying the HTTP
  status and the WordPress error body.
