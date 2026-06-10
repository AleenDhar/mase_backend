# DeepAgent Server - Technical Documentation

## 1. Overview

DeepAgent Server is an AI-powered agentic server built on the LangGraph DeepAgents framework. It orchestrates multiple AI models (Claude, GPT-4, Gemini) with integrated MCP (Model Context Protocol) servers for Salesforce, Clay, and Avoma. The server provides real-time streaming, background task execution, Supabase persistence, and context window management for long-running AI agent tasks.

**Base URL:** `https://agent-salesforce-link.replit.app`

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Server (port 5000)            │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐    │
│  │ /api/chat │  │/api/chat │  │ /api/chat/structured│   │
│  │ (stream)  │  │ /async   │  │  (JSON output)      │   │
│  └─────┬─────┘  └─────┬────┘  └─────────┬──────────┘   │
│        │              │                  │               │
│        └──────────────┼──────────────────┘               │
│                       ▼                                  │
│              ┌────────────────┐                          │
│              │  DeepAgent     │                          │
│              │  (LangGraph)   │                          │
│              └───────┬────────┘                          │
│                      │                                   │
│        ┌─────────────┼─────────────┐                    │
│        ▼             ▼             ▼                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │Salesforce│  │   Clay   │  │  Avoma   │              │
│  │   MCP    │  │   MCP    │  │   MCP    │              │
│  │ (stdio)  │  │ (stdio)  │  │ (stdio)  │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│        │             │             │                    │
│        ▼             ▼             ▼                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │Salesforce│  │ Clay API │  │Avoma API │              │
│  │   Org    │  │          │  │          │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                         │
│  ┌─────────────────────┐  ┌───────────────────┐        │
│  │  Context Window Mgr │  │  Supabase Client  │        │
│  │  (GPT-4o-mini)      │  │  (Real-time save) │        │
│  └─────────────────────┘  └───────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

---

## 3. File Structure

```
/
├── server.py                  # Main FastAPI server (all endpoints, agent management)
├── salesforce_mcp_server.py   # Salesforce MCP server (SOQL, records, search)
├── avoma_mcp_server.py        # Avoma MCP server (meetings, transcripts, notes)
├── mcp_config.json            # MCP server configuration
├── custom_tools/              # Custom LangChain tools directory
│   └── example_tools.py       # Example custom tool (calculator)
├── replit.md                  # Project metadata and preferences
└── TECHNICAL_DOCUMENTATION.md # This document
```

---

## 4. API Endpoints

### 4.1 Chat Endpoints

#### `POST /api/chat` - Streaming Chat

Primary chat endpoint. Runs the agent as a background task, streams events to the client, and saves all events to Supabase. If the client disconnects, the agent continues running and saves results to Supabase.

**Request Body:**
```json
{
  "messages": [
    {"role": "user", "content": "Your message here"}
  ],
  "stream": true,
  "model": "anthropic:claude-sonnet-4-20250514",
  "system_prompt": "Optional system prompt",
  "headless": true,
  "chat_id": "optional-uuid",
  "google_sheets": null
}
```

**Parameters:**
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| messages | array | required | Array of {role, content} message objects |
| stream | boolean | true | Enable SSE streaming |
| model | string | null | Override model (e.g. "openai:gpt-4o", "anthropic:claude-sonnet-4-20250514") |
| system_prompt | string | null | Custom system instructions |
| headless | boolean | true | Browser control mode |
| chat_id | string | auto-generated UUID | Client-provided chat ID for tracking |
| google_sheets | array | null | Google Sheets config for data access |

**SSE Stream Events:**
```
data: {"type": "chat_id", "chat_id": "uuid"}
data: {"type": "tool_call", "tool": "soql", "args": {...}}
data: {"type": "thinking", "content": "Calling soql..."}
data: {"type": "tool_result", "tool": "soql", "result": "..."}
data: {"type": "token", "content": "partial response..."}
data: {"type": "final", "content": "complete response"}
data: {"type": "error", "content": "error message"}
data: {"type": "status", "content": "Agent stopped by user.", "status": "cancelled"}
```

**Non-Streaming Response (stream=false):**
```json
{
  "chat_id": "uuid",
  "response": "Agent's complete response",
  "done": true
}
```

---

#### `POST /api/chat/async` - Async Chat (Long-Running Tasks)

Designed for tasks that may take 5+ minutes. Runs the agent in the background, sends keepalive pings to keep the connection alive, and saves all results to Supabase. Client reads results via Supabase realtime subscription.

**Request Body:** Same as `/api/chat`

**SSE Stream:**
```
data: {"type": "chat_id", "chat_id": "uuid"}
: keepalive                                    (every 15 seconds)
data: {"type": "done", "chat_id": "uuid"}
```

**Usage Pattern:**
1. Client sends request, receives `chat_id`
2. Client subscribes to Supabase realtime on `chat_messages` table filtered by `chat_id`
3. Agent runs in background, saves all events to Supabase
4. Client reads events from Supabase in real-time

---

#### `POST /api/chat/stop?chat_id=XXX` - Stop Agent Task

Stops a running agent task. The agent will finish its current tool call and then stop.

**Query Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| chat_id | string | yes | The chat_id of the task to stop |

**Response:**
```json
{
  "chat_id": "uuid",
  "status": "stopping",
  "message": "Agent is being stopped. It will finish the current tool call and then stop."
}
```

**Possible Statuses:** `stopping`, `already_done`, `not_found`

---

#### `GET /api/chat/active` - List Active Tasks

Returns all currently running agent tasks.

**Response:**
```json
{
  "active_chats": [
    {"chat_id": "uuid", "status": "running"}
  ],
  "count": 1
}
```

---

#### `POST /api/chat/structured` - Structured JSON Output

Returns agent response as structured JSON matching a provided schema.

**Request Body:**
```json
{
  "messages": [{"role": "user", "content": "..."}],
  "structured_output_format": {
    "type": "object",
    "properties": {
      "field_name": {"type": "string", "description": "..."}
    }
  },
  "system_prompt": "Optional",
  "model": "Optional"
}
```

---

### 4.2 WebSocket Endpoint

#### `WS /ws/chat` - WebSocket Chat

Full-duplex chat via WebSocket.

**Send:**
```json
{
  "messages": [{"role": "user", "content": "..."}],
  "model": "optional"
}
```

**Receive:** Same event types as SSE streaming.

---

### 4.3 System Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check with agent status, model, and config |
| `/api/config` | GET | Current server configuration |
| `/api/tools` | GET | List all available tools (25 total) |
| `/api/mcp/servers` | GET | List configured MCP servers |
| `/api/mcp/servers/{name}` | POST | Add a new MCP server |
| `/api/mcp/servers/{name}` | DELETE | Remove an MCP server |

---

## 5. MCP Servers & Tools

### 5.1 Salesforce MCP (5 tools)

Connects to your Salesforce org via `simple-salesforce`.

| Tool | Description |
|------|-------------|
| `soql` | Run SOQL queries (e.g. `SELECT Id, Name FROM Account`) |
| `get_record` | Retrieve a single record by object type and ID |
| `describe_object` | Get object metadata (fields, types, relationships) |
| `list_objects` | List all Salesforce objects in the org |
| `search` | SOSL full-text search across objects |

**Authentication:** Uses `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN`, `SF_DOMAIN` environment variables.

---

### 5.2 Clay MCP (11 tools)

Connects to Clay's API for contact enrichment and data management.

| Tool | Description |
|------|-------------|
| `searchContacts` | Search contacts by criteria |
| `searchInteractions` | Search interaction history |
| `aggregateContacts` | Aggregate contact data |
| `getContact` | Get a specific contact |
| `createContact` | Create a new contact |
| `createNote` | Create a note |
| `getGroups` | List groups |
| `createGroup` | Create a group |
| `updateGroup` | Update a group |
| `getNotes` | List notes |
| `getEvents` | List events |

**Authentication:** Uses `CLAY_API_KEY` environment variable.

---

### 5.3 Avoma MCP (6 tools)

Connects to Avoma's API for meeting intelligence. Supports CRM (Salesforce) filters.

| Tool | Description |
|------|-------------|
| `list_meetings` | List meetings with filters (CRM account, opportunity, contact, lead, attendees, date range) |
| `get_meeting` | Get detailed meeting information |
| `get_meeting_transcript` | Get full meeting transcript |
| `get_meeting_notes` | Get AI-generated meeting notes (json, html, or markdown) |
| `get_meeting_insights` | Get AI insights, keywords, and speaker data |
| `get_meeting_segments` | Get meeting segments (intro, demo, pricing, next steps) |

**`list_meetings` Filter Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| from_date | string | Start date (ISO format, required) |
| to_date | string | End date (ISO format, required) |
| crm_account_ids | string | Comma-separated Salesforce Account IDs |
| crm_opportunity_ids | string | Comma-separated Salesforce Opportunity IDs |
| crm_contact_ids | string | Comma-separated CRM Contact IDs |
| crm_lead_ids | string | Comma-separated CRM Lead IDs |
| attendee_emails | string | Comma-separated attendee emails |
| is_internal | boolean | Filter internal vs external meetings |
| is_call | boolean | Filter voice calls vs video calls |
| include_crm_associations | boolean | Include CRM associations in response |
| page_size | int | Results per page (max 100) |
| order | string | Sort order: "start_at" or "-start_at" |

**Error Handling:** All Avoma tools return `{"error": "..."}` on failure instead of crashing, allowing the agent to continue working.

---

### 5.4 Built-in Tools (3 tools)

| Tool | Description |
|------|-------------|
| `duckduckgo_search` | Web search via DuckDuckGo |
| `get_current_time` | Get current date and time |
| `example_calculator` | Example calculator tool |

---

## 6. Context Window Management

The server manages context windows to prevent token overflow during long agent runs.

### 6.1 Tool Response Summarization
- **Threshold:** 50,000 characters
- **Model:** GPT-4o-mini
- When a tool returns a response exceeding the threshold, it's summarized before entering the message history
- Full raw responses are preserved on disk

### 6.2 Conversation History Summarization
- **Token Threshold:** 100,000 tokens (estimated at 4 chars/token)
- **Keep Recent:** 20 most recent messages
- When conversation history exceeds the threshold, older messages are summarized into a compact summary
- Recent messages are preserved in full

### 6.3 MCP Response Truncation
- **Max Response Size:** 500,000 characters
- **Max String Length:** 50,000 characters per field
- **Max List Items:** 100 items per list

---

## 7. Supabase Integration

All agent events are persisted to Supabase in real-time for client consumption.

### 7.1 Table Schema: `chat_messages`

| Column | Type | Description |
|--------|------|-------------|
| chat_id | text | UUID identifying the chat session |
| role | text | Always "assistant" |
| type | text | Event type (see below) |
| content | text | Event content |
| sequence | integer | Monotonically increasing sequence number per chat_id |
| metadata | text/jsonb | Additional metadata (nullable) |

### 7.2 Event Types

| Type | Description | Metadata |
|------|-------------|----------|
| status | Processing status changes | `{"status": "started"}` or `{"status": "cancelled"}` |
| tool_call | Agent is calling a tool | `{"tool": "soql", "args": {...}, "step": 1}` |
| thinking | Agent thinking/reasoning | `{"tool": "soql", "step": 1}` |
| tool_result | Tool execution result | `{"tool": "soql"}` |
| token | Partial AI response | none |
| final | Complete final response | `{"tool_calls": 5, "status": "completed"}` |
| error | Error occurred | `{"status": "failed"}` |

### 7.3 Client Integration Pattern

```javascript
// 1. Start agent task
const response = await fetch('/api/chat/async', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    messages: [{role: 'user', content: 'Analyze Acme Corp'}],
    chat_id: 'my-chat-id'
  })
});

// 2. Subscribe to Supabase realtime
const subscription = supabase
  .channel('chat')
  .on('postgres_changes', {
    event: 'INSERT',
    schema: 'public',
    table: 'chat_messages',
    filter: `chat_id=eq.my-chat-id`
  }, (payload) => {
    const event = payload.new;
    console.log(`[${event.type}] ${event.content}`);
  })
  .subscribe();

// 3. Stop if needed
await fetch('/api/chat/stop?chat_id=my-chat-id', {method: 'POST'});
```

---

## 8. Supported AI Models

| Provider | Model ID | Description |
|----------|----------|-------------|
| Anthropic | `anthropic:claude-sonnet-4-20250514` | Default model, Claude Sonnet 4 |
| Anthropic | `anthropic:claude-opus-4-20250514` | Claude Opus 4 |
| OpenAI | `openai:gpt-4o` | GPT-4o |
| OpenAI | `openai:gpt-4o-mini` | GPT-4o Mini (used for summarization) |
| Google | `google:gemini-pro` | Gemini Pro |

Pass the model ID in the `model` field of chat requests to switch models per request.

---

## 9. Environment Variables

### Required Secrets

| Variable | Description |
|----------|-------------|
| ANTHROPIC_API_KEY | Anthropic API key for Claude models |
| OPENAI_API_KEY | OpenAI API key for GPT models and summarization |
| SUPABASE_URL | Supabase project URL |
| SUPABASE_SERVICE_KEY | Supabase service role key |
| SF_PASSWORD | Salesforce password |
| SF_SECURITY_TOKEN | Salesforce security token |
| CLAY_API_KEY | Clay API key |
| SESSION_SECRET | Session secret for server |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| SF_USERNAME | (set in env) | Salesforce username |
| SF_DOMAIN | login | Salesforce domain (login or test) |

### Optional Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| MODEL | anthropic:claude-sonnet-4-20250514 | Default AI model |
| PORT | 5000 | Server port |
| MCP_MAX_RESPONSE_SIZE | 500000 | Max MCP response size (chars) |
| MCP_MAX_STRING_LENGTH | 50000 | Max string field length (chars) |
| MCP_MAX_LIST_ITEMS | 100 | Max list items per response |
| SUMMARIZER_MODEL | gpt-4o-mini | Model for context summarization |
| TOOL_RESPONSE_SUMMARIZE_THRESHOLD | 50000 | Summarize tool responses above this (chars) |
| CONVERSATION_SUMMARIZE_TOKEN_THRESHOLD | 100000 | Summarize conversation above this (tokens) |
| CONVERSATION_KEEP_RECENT_MESSAGES | 20 | Keep this many recent messages |

---

## 10. Cross-Platform Workflow: Salesforce + Avoma

The agent can combine Salesforce and Avoma data in a single request. Here's the recommended flow:

### Get Meetings for a Salesforce Account
1. Agent uses `soql` to find Account ID: `SELECT Id FROM Account WHERE Name LIKE '%Acme%'`
2. Agent uses `list_meetings` with `crm_account_ids` filter
3. Agent uses `get_meeting_notes` or `get_meeting_transcript` for details

### Get Meetings for a Salesforce Opportunity
1. Agent uses `soql` to find Opportunity ID: `SELECT Id FROM Opportunity WHERE Name LIKE '%Renewal%'`
2. Agent uses `list_meetings` with `crm_opportunity_ids` filter
3. Agent uses `get_meeting_insights` for AI analysis

---

## 11. Error Handling

### MCP Tool Errors
- All MCP tools return `{"error": "..."}` on failure instead of throwing exceptions
- The agent receives the error message and can adapt (try different tools, skip, or inform the user)
- Avoma API errors (404, 400, etc.) are caught and returned as structured error objects

### Agent Task Errors
- Errors during agent execution are saved to Supabase with type `error`
- The client receives an error event via SSE stream
- Stack traces are logged server-side with `[STREAM ERROR]` or `[AGENT TASK]` prefixes

### Agent Task Cancellation
- Users can stop running tasks via `POST /api/chat/stop?chat_id=XXX`
- The agent finishes its current tool call, then stops gracefully
- A `cancelled` status is saved to Supabase
- Streaming clients receive a status event with `"status": "cancelled"`

---

## 12. No-Timeout Requirement

The API must have zero timeouts at every layer. Agent tasks can run for 30+ minutes and must never be interrupted.

### How No-Timeout Is Achieved

| Layer | Mechanism |
|-------|-----------|
| **HTTP Connection** | SSE keepalive pings every 15 seconds prevent proxy/load balancer timeouts |
| **Agent Execution** | Agent runs as a background asyncio task, not tied to the HTTP request lifecycle |
| **Client Disconnect** | If the client disconnects, the agent continues running in the background |
| **Data Persistence** | All events saved to Supabase in real-time; no data lost even on full disconnect |
| **Platform** | Reserved VM deployment (always-on, no platform-enforced request timeout) |

### Design Principles
1. The HTTP response is just a delivery mechanism - the agent task is independent
2. Even if the HTTP stream breaks, the agent completes its work and saves to Supabase
3. The client can reconnect and read missed events from Supabase by querying `chat_messages` filtered by `chat_id`
4. No uvicorn, FastAPI, or application-level timeouts are set on agent execution
5. The `/api/chat/async` endpoint is specifically designed for tasks exceeding 5 minutes

### Why Reserved VM (Not Autoscale)
- Autoscale has a hard 5-minute HTTP request timeout that cannot be bypassed
- Reserved VM has no platform-level timeout on requests
- Reserved VM is always running, so there is no cold start delay

---

## 13. Deployment

- **Platform:** Replit (Reserved VM)
- **Runtime:** Python 3.11, Node.js 20
- **Server:** FastAPI + Uvicorn on port 5000
- **Deployment Type:** Reserved VM (always-on, no timeout limits)
- **Deploy Command:** `python3 server.py`

### Production Considerations
- Reserved VM avoids the 5-minute HTTP timeout of Autoscale
- Agent tasks can run for 30+ minutes without interruption
- All results persisted to Supabase for reliability
- Client disconnections do not affect running agent tasks
