# External Dependencies
Core: DeepAgents · FastAPI · Uvicorn · LangChain MCP Adapters · FastMCP · LangChain Anthropic / OpenAI · Supabase · psutil.

Embeddings: OpenAI `text-embedding-ada-002`.

MCP servers (one Python file each unless noted): `salesforce_mcp_server.py` (simple-salesforce), `avoma_mcp_server.py`, `zoominfo_mcp_server.py`, `seamlessai_mcp_server.py`, `wiza_mcp_server.py`, `lusha_mcp_server.py`, `showpad_mcp_server.py`, Apollo (`@thevgergroup/apollo-io-mcp` via npx).

## Lemlist (`lemlist_mcp_server.py`, 48 tools)
Local FastMCP server, Basic auth. Tool groups: campaigns, leads, activities, unsubscribes, enrichment, webhooks, tasks, people search, plus reporting (`lemlist_bounce_breakdown`, `lemlist_sender_performance`, `lemlist_step_breakdown`, `lemlist_classify_replies` — uses Claude Haiku, model overridable via `LEMLIST_REPLY_CLASSIFIER_MODEL`).

Notable behaviour:
- Thread-safe queueing (`threading.Lock`) + auto-retry on 429 (honours `Retry-After`) and auto pause/retry/resume on 500 for running campaigns. Server-side `asyncio.Semaphore(1)` in `server.py` serialises parallel Lemlist calls.
- `lemlist_activity_summary_by_user` batch-resolves any `usr_xxx` IDs not in `/team/senders` via `/users/{uid}` (cached in `_user_name_cache` for the process lifetime).
- Enrichment (`lemlist_enrich_lead`, `lemlist_get_enrichment_result`) uses async `GET /enrich` with built-in polling up to 30s.
- Reporting tools accept `YYYY-MM-DD` and ISO-8601 via `_parse_date_param`.
- `lemlist_sanitizer.py` is disabled — Lemlist handles raw Unicode (umlauts, em dashes) natively.
- **Salesforce auto-sync:** When a lead is added to a campaign, `Contact.Lemlist_Campaign_Added_Date__c` is set to the UTC timestamp.
- **Validated push + receipts (2026-05-20):** `lemlist_validated_push` and `lemlist_get_push_receipts` write/read `public.lemlist_push_receipts` (indexed on `(chat_id, created_at desc)`, `(campaign_id, created_at desc)`, `(email)`). Each receipt: `chat_id`, `account_id`, `campaign_id`, `owner_email`, `owner_user_id`, `sf_contact_id`, `sf_account_id`, `email`, `action`, `http_status`, `lemlist_lead_id`, `api_method`, `api_endpoint`, `error`, `payload`, `created_at`. Failure receipts now parse HTTP status out of the error string into `http_status`. Required env (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`) declared in `mcp_config.json` under `mcp_servers.lemlist.env` — `MultiServerMCPClient` does NOT propagate parent env.
- **`chat_id` injection (2026-05-22):** `AgentManager._wrap_mcp_tool` overrides the `chat_id` argument for `lemlist_validated_push` and `lemlist_get_push_receipts` using the real chat UUID from `_current_chat_id` ContextVar. Tool schema retains the param so external `/mcp` clients can supply it; in-agent calls log `[CHAT-ID-INJECT]` on swap. Override also rewrites the top-level `chat_id` in the return payload (dict, JSON-string, and MCP content-block shapes). Root cause: chat `c9e501c6` Sonnet fabricated `chat_id="current_session_aaf_001"` and 11 receipts ended up unfindable.

## LinkedIn Ads (`linkedin_mcp_server.py`, 7 tools)
OAuth2 via `LINKEDIN_ACCESS_TOKEN` (scopes `r_ads` + `r_ads_reporting`). Versioned REST: `LinkedIn-Version: 202602`, `X-Restli-Protocol-Version: 2.0.0`. v202602 requires account-scoped URLs: `/rest/adAccounts/{id}/adCampaigns`, `/rest/adAccounts/{id}/adCampaignGroups`. Analytics: `GET /rest/adAnalytics?q=analytics` with RestLi `dateRange=(start:(year:Y,month:M,day:D),end:(...))` — built via raw `httpx build_request+send` to prevent URL-encoding of RestLi syntax. Valid pivots in v202602: CAMPAIGN, CREATIVE, CAMPAIGN_GROUP, ACCOUNT, COMPANY, MEMBER_COMPANY_SIZE, MEMBER_INDUSTRY, MEMBER_JOB_FUNCTION, MEMBER_JOB_TITLE, MEMBER_SENIORITY. **MEMBER_COUNTRY/MEMBER_REGION are NOT valid** (FIELD_INVALID); for regional analysis, group campaigns via `targetingCriteria` location URNs and aggregate per-campaign. Features: token caching, auto-pagination for campaigns and analytics, explicit `fields` including `costInLocalCurrency`.

## Eloqua (`eloqua_mcp_server.py`, 19 tools)
Basic Auth with compound username `SITE\USER:PASS`; base URL resolved at startup from `https://login.eloqua.com/id`; all calls `{base_url}/API/REST/2.0/`. Env: `ELOQUA_SITE_NAME`, `ELOQUA_USERNAME`, `ELOQUA_PASSWORD`.

Reporting:
- `eloqua_get_email_performance` — rows match standard Google Sheet headers (Send Date, Campaign Name, Subject, Sends, Delivered/Open/Click/Bounce/Unsubscribe Rates).
- `eloqua_get_email_deployment` — raw data for a single send.
- `eloqua_get_campaign_email_report` — primary for batch campaigns (US_ENT_, APAC_, EU_, MM_); uses Bulk API (deployments endpoint only covers quick/form-triggered). Exports 5 activity types, aggregates by campaign+email, computes unique opens/clicks (distinct ContactId), splits bounces hard (5xx) vs soft (4xx).
- **Bulk API gotcha:** activity type names differ from display names. `EmailSend`, `EmailOpen`, `EmailClickthrough` are correct; bounces are `Bounceback` (NOT `EmailBounceback`) and unsubscribes are `Unsubscribe` (NOT `EmailUnsubscribe`) — prefixed names return HTTP 400 "Unknown Activity.Type filter".

Filter syntax: `'{{Activity.Type}}'='EmailSend' AND '{{Activity.CreatedAt}}'>='YYYY-MM-DD HH:MM:SS'`.

## Showpad (`showpad_mcp_server.py`)
v4 REST (`zycus.api.showpad.com/v4`) + v3 (`zycus.showpad.biz/api/v3`), `SHOWPAD_API_KEY` Bearer (read-only). Metadata tools: `list_assets`, `get_asset`, `search_assets`, `query_assets` (ShowQL), `search_tags`, divisions/shared-spaces/channels/users.
- **`get_asset_content(asset_id, offset, limit)`** — downloads an asset and returns its EXTRACTED TEXT (not just metadata). Resolves the file link from the v3 `assets/{id}.json` `downloadLink` + `extension`/`filetype`, streams the bytes with the Bearer header (capped by `SHOWPAD_CONTENT_MAX_BYTES`, default 60 MB), and dispatches to a type-specific extractor: PDF (`pypdf`), PPTX (`python-pptx`, incl. tables + speaker notes), DOCX (`python-docx`, incl. tables), XLSX (`openpyxl`), and plain-text/HTML (TXT/MD/CSV/TSV/JSON/XML/HTML — HTML stripped via BeautifulSoup). Output paginated by character: at most `limit` chars (default/cap `SHOWPAD_CONTENT_MAX_CHARS`=20000) from `offset`; `truncated`/`next_offset` page further. Unsupported types (legacy .ppt/.doc/.xls, images, video), missing download links, auth, and parse failures return an explicit `error` (no OCR; image-only PDFs yield no text). Discoverable through the `/mcp` gateway like every other MCP tool.

## Other
- **Clay webhooks** (`send_to_clay`): requires `CLAY_WEBHOOK_URL`.
- All authenticated APIs use environment variables for keys/secrets.
