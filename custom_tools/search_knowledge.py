import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rag_context import current_project_id as _current_project_id, current_chat_id as _current_chat_id_for_rag

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Per-turn search_knowledge budget — caps RAG over-fetching that bloats
# context and (per chat f4c06387) makes downstream LLM calls large enough
# to hang. Counter is keyed by chat_id and reset at the start of each
# agent run via reset_search_knowledge_counter(chat_id).
MAX_SEARCH_KNOWLEDGE_PER_TURN = int(
    os.environ.get("MAX_SEARCH_KNOWLEDGE_PER_TURN", "100"))

# Hard-stop escalation (added 2026-05-22 after chat 8359d7a6 burned $8.02
# on RAG loops). When the cap fires repeatedly, returning an error string
# to the LLM is too soft a signal — Sonnet 4.6 keeps fan-out generating
# new parallel search_knowledge tool_use blocks each turn ("try a
# different query"), and each rejected turn still costs ~$1 in input
# tokens. After this many cap-blocked calls in a single chat, we forcibly
# cancel the in-process agent task via server.cancel_running_chat so the
# next LLM turn never fires.
MAX_SK_CAP_HITS_BEFORE_CANCEL = int(
    os.environ.get("MAX_SK_CAP_HITS_BEFORE_CANCEL", "5"))

_sk_counter_lock = threading.Lock()
# Run-scoped dedupe memory (NOT reset between auto-continue steps) so the
# SAME query is still blocked as a duplicate across a whole multi-step run.
_sk_seen_queries: Dict[str, List[str]] = {}
# Per-MODEL-STEP count of accepted searches. The cap is enforced per agent
# step (one auto-continue iteration) rather than across a whole run, so a
# legitimately RAG-heavy workflow spread over many steps does not accumulate
# into the cap and trip the runaway-loop cancel. Reset every step via
# reset_search_knowledge_step().
_sk_step_count: Dict[str, int] = {}
# Cumulative count of cap-blocked attempts per chat_id (not reset by
# dedupe blocks — only true cap blocks count). When this reaches
# MAX_SK_CAP_HITS_BEFORE_CANCEL we escalate. Reset every step too, so the
# cancel only fires when a SINGLE step keeps spamming after being told to stop.
_sk_cap_hits: Dict[str, int] = {}
# Chat IDs we have already cancelled for SK abuse so we don't fire the
# cancel hook repeatedly for the same chat (each subsequent parallel
# tool_use still tries to invoke the tool until the loop unwinds).
_sk_cancelled: set = set()


def _normalize_query(q: str) -> str:
    """Bag-of-words normalisation so near-identical queries dedupe."""
    tokens = re.findall(r"[a-z0-9]+", (q or "").lower())
    return " ".join(sorted(set(t for t in tokens if len(t) > 2)))


# Sentinel bucket used when no chat_id is in scope. Without this the cap
# silently bypassed (see chat 88f73936: 30 search_knowledge calls in a single
# turn). The ContextVar in rag_context can be empty if the tool runs outside
# the asyncio context where _set_rag_context was called — instead of waving
# such calls through we lump them into one shared bucket so the cap still
# fires.
_SK_NO_CHAT_BUCKET = "__no_chat_id__"


def reset_search_knowledge_counter(chat_id: str) -> None:
    """Full reset — called by server.py at the start/end of every agent run.
    Clears the run-scoped dedupe memory AND the per-step cap counter."""
    with _sk_counter_lock:
        if chat_id:
            _sk_seen_queries.pop(chat_id, None)
            _sk_step_count.pop(chat_id, None)
            _sk_cap_hits.pop(chat_id, None)
            _sk_cancelled.discard(chat_id)
        # Also clear the fallback bucket so a missing-chat_id run can't
        # poison the next run's budget.
        _sk_seen_queries.pop(_SK_NO_CHAT_BUCKET, None)
        _sk_step_count.pop(_SK_NO_CHAT_BUCKET, None)
        _sk_cap_hits.pop(_SK_NO_CHAT_BUCKET, None)


def reset_search_knowledge_step(chat_id: str) -> None:
    """Lighter reset — called by server.py at the start of each auto-continue
    model step. Resets ONLY the per-step cap counter and cap-hit tally so each
    step gets a fresh budget of MAX_SEARCH_KNOWLEDGE_PER_TURN searches.

    Deliberately keeps the run-scoped dedupe memory (_sk_seen_queries) so the
    SAME query is still blocked as a duplicate across steps, and keeps
    _sk_cancelled so a cancelled run stays cancelled."""
    with _sk_counter_lock:
        for bucket in {chat_id or _SK_NO_CHAT_BUCKET, _SK_NO_CHAT_BUCKET}:
            _sk_step_count.pop(bucket, None)
            _sk_cap_hits.pop(bucket, None)


def _fire_cancel(chat_id: str) -> bool:
    """Lazy-import + call server.cancel_running_chat to terminate the
    runaway agent loop. Returns True iff a task was actually cancelled.
    Imported lazily to avoid a server <-> custom_tools circular import at
    module load."""
    try:
        from server import cancel_running_chat as _cancel  # noqa: WPS433
        return bool(_cancel(chat_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[SK_CAP] cancel hook failed for chat={chat_id}: {exc}")
        return False


def _check_and_record_sk_call(chat_id: str, query: str) -> Optional[str]:
    """Soft governor for search_knowledge. Returns a short guidance string when a
    call should be SOFT-blocked (an exact-duplicate query, or beyond a generous
    per-step budget), or None to let it proceed.

    IMPORTANT: this NEVER cancels or terminates the agent. Earlier versions
    hard-cancelled the run after repeated cap hits, which crashed long, legitimately
    RAG-heavy workflows (e.g. ABM) mid-flight and lost their context. The cap is now
    a high, advisory soft limit: when exceeded we just ask the model to proceed with
    what it has and return the result text — a safe fallback, never a crash."""
    bucket = chat_id or _SK_NO_CHAT_BUCKET
    if not chat_id:
        print(f"[SK_CAP] WARNING: search_knowledge called with no chat_id; "
              f"using shared fallback bucket. query={query[:80]!r}")
    norm = _normalize_query(query)
    with _sk_counter_lock:
        seen = _sk_seen_queries.setdefault(bucket, [])
        if norm and norm in seen:
            # Exact-duplicate dedupe — cheap, prevents wasted identical queries.
            print(f"[SK_CAP] dedupe (duplicate) chat={chat_id or '<none>'} "
                  f"query={query[:80]!r}")
            return (
                "Duplicate search_knowledge query (already executed this turn). "
                "Reuse the prior results instead of re-querying — proceed."
            )
        count = _sk_step_count.get(bucket, 0)
        if count >= MAX_SEARCH_KNOWLEDGE_PER_TURN:
            # Soft cap only — advise, never terminate.
            print(f"[SK_CAP] soft cap reached (cap={MAX_SEARCH_KNOWLEDGE_PER_TURN}) "
                  f"chat={chat_id or '<none>'} prior={count} query={query[:80]!r}")
            return (
                f"You have already run {count} knowledge searches this step "
                f"(soft cap {MAX_SEARCH_KNOWLEDGE_PER_TURN}). You almost certainly "
                f"have enough context now — proceed with what you have, and only "
                f"search again for a specific fact that is genuinely still missing. "
                f"Already-queried this run: {seen[-10:]}"
            )
        seen.append(norm)
        _sk_step_count[bucket] = count + 1
        return None


def _get_embedding(text: str) -> List[float]:
    import httpx
    r = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": "text-embedding-ada-002", "input": text},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


def _supabase_rpc(function_name: str, params: dict) -> Any:
    import httpx
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{function_name}",
        headers=headers,
        json=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _supabase_query(table: str, params: dict) -> Any:
    import httpx
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=headers,
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


_FILENAME_EXTENSIONS = {".md", ".pdf", ".csv", ".txt", ".docx", ".xlsx", ".json"}


def _is_filename_query(query: str) -> bool:
    """Return True if the query looks like a filename rather than a search query."""
    q = query.strip()
    for ext in _FILENAME_EXTENSIONS:
        if q.lower().endswith(ext):
            return True
    return False


def _resolve_document_ids_by_name(name_filter: str, project_id: str) -> List[str]:
    """Return document IDs whose name contains name_filter (case-insensitive)."""
    docs = _supabase_query("documents", {
        "select": "id",
        "project_id": f"eq.{project_id}",
        "name": f"ilike.*{name_filter}*",
    })
    return [d["id"] for d in docs if d.get("id")]


def _list_available_document_names(project_id: str) -> List[str]:
    """Return list of document names in the project."""
    try:
        docs = _supabase_query("documents", {
            "select": "name",
            "project_id": f"eq.{project_id}",
        })
        return [d["name"] for d in docs if d.get("name")]
    except Exception:
        return []


def _search_chunks_in_documents(
    embedding: List[float], doc_ids: List[str], max_results: int
) -> List[dict]:
    """Fetch ALL chunks for the given document IDs and rank by cosine similarity in Python.

    This is the pre-filter path used when document_name is supplied. The global RPC
    match_document_chunks returns the top-k across the entire project (e.g. 535 chunks).
    For small documents (e.g. 8 chunks) those chunks rarely appear in the global top-k,
    so post-filtering by document_id returns nothing. By fetching only the target
    document's chunks and ranking them here, we guarantee the right document is searched.

    text-embedding-ada-002 embeddings are unit-normalised, so dot product == cosine sim.
    """
    all_chunks: List[dict] = []
    for doc_id in doc_ids:
        rows = _supabase_query("document_chunks", {
            "select": "id,document_id,project_id,content,embedding",
            "document_id": f"eq.{doc_id}",
        })
        all_chunks.extend(rows if isinstance(rows, list) else [])

    results = []
    for chunk in all_chunks:
        raw_emb = chunk.get("embedding")
        if raw_emb is None:
            continue
        if isinstance(raw_emb, str):
            try:
                raw_emb = json.loads(raw_emb)
            except Exception:
                continue
        try:
            similarity = sum(a * b for a, b in zip(embedding, raw_emb))
        except Exception:
            continue
        results.append({
            "id": chunk.get("id"),
            "document_id": chunk.get("document_id"),
            "project_id": chunk.get("project_id"),
            "content": chunk.get("content", ""),
            "similarity": similarity,
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:max_results]


@tool
def search_knowledge(query: str, max_results: int = 5, document_name: str = "") -> str:
    """Search the knowledge base for relevant documents and information.

    Use this tool to find information from uploaded documents available in the
    current project. Searches document chunks using semantic similarity.

    To search within a specific file, pass document_name (partial name is fine,
    case-insensitive). For example: document_name="Communication_Intelligence_Matrix"
    will restrict results to that file only.

    To verify which files are indexed, use the list_documents tool instead of
    searching by filename here.

    IMPORTANT: The `query` parameter must describe the CONTENT you are looking for
    (e.g. "email opening moves structure" or "value propositions for procurement").
    Do NOT pass a filename as the query — use the `document_name` parameter for that.
    If you accidentally pass a filename as `query`, the tool will auto-correct.

    Args:
        query: The search query describing what information you're looking for.
            Do NOT pass a filename here — use document_name for file filtering.
        max_results: Maximum number of relevant chunks to return (1-20, default 5).
        document_name: Optional. Filter results to chunks from a specific document.
            Partial, case-insensitive match against the document filename.

    Returns:
        JSON string with matching document chunks, their content, similarity scores,
        and source document names.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return json.dumps({"error": "Supabase not configured."})
    if not OPENAI_API_KEY:
        return json.dumps({"error": "OpenAI API key not configured (needed for embeddings)."})

    project_id = _current_project_id.get(None)
    chat_id = _current_chat_id_for_rag.get(None)

    if not project_id and not chat_id:
        return json.dumps({"error": "No project context available. This tool requires a project_id or chat_id to scope the search."})

    # Per-turn cap + dedupe (see chat f4c06387 hang post-mortem):
    cap_err = _check_and_record_sk_call(chat_id, query)
    if cap_err:
        return json.dumps({"error": cap_err, "query": query})

    max_results = max(1, min(20, max_results))

    # Auto-correct: if the query looks like a filename (e.g. "ABM_Email_Framework.md"),
    # treat it as a document_name filter and use a broad content query instead.
    # This prevents the common mistake of passing a filename as the semantic query.
    # Strip the file extension so "General.md" still matches "General_v6.5.md" via ilike.
    autocorrected = False
    if _is_filename_query(query) and not document_name:
        raw_name = query.strip()
        # Strip the extension to improve partial matching (e.g. "Framework.md" → "Framework")
        import os as _os
        document_name = _os.path.splitext(raw_name)[0]
        query = "content overview structure sections"
        autocorrected = True

    try:
        embedding = _get_embedding(query)

        # If document_name filter given, use pre-filter path to avoid the global top-k problem.
        # The global RPC ranks across ALL project chunks (e.g. 535). A small document with
        # 8 chunks rarely appears in the global top-k, so post-filtering would return nothing.
        # Instead: resolve the document IDs, fetch ONLY those chunks, rank in Python.
        if document_name and project_id:
            matched_ids = _resolve_document_ids_by_name(document_name.strip(), project_id)
            if not matched_ids:
                available = _list_available_document_names(project_id)
                return json.dumps({
                    "message": f"No document found matching '{document_name}'. Use list_documents to see available files.",
                    "query": query,
                    "document_name_filter": document_name,
                    "available_documents": available,
                })
            # Pre-filter: fetch only chunks from the matched documents and rank locally.
            all_results = _search_chunks_in_documents(embedding, matched_ids, max_results)

        else:
            # No document_name filter — use the global RPC as before.
            all_results = []
            fetch_count = max_results

            if project_id:
                matches = _supabase_rpc("match_document_chunks", {
                    "query_embedding": embedding,
                    "match_threshold": -1.0,
                    "match_count": fetch_count,
                    "match_project_id": project_id,
                })
                all_results.extend(matches if isinstance(matches, list) else [])

            if chat_id:
                try:
                    chat_docs = _supabase_query("chat_documents", {
                        "select": "document_id",
                        "chat_id": f"eq.{chat_id}",
                    })
                    chat_doc_ids = [d["document_id"] for d in chat_docs]

                    if chat_doc_ids:
                        existing_result_ids = {r.get("id") for r in all_results}
                        for doc_id in chat_doc_ids:
                            doc_result = _supabase_query("document_chunks", {
                                "select": "project_id",
                                "document_id": f"eq.{doc_id}",
                                "limit": "1",
                            })
                            if doc_result:
                                chunk_project_id = doc_result[0].get("project_id")
                                if chunk_project_id:
                                    matches = _supabase_rpc("match_document_chunks", {
                                        "query_embedding": embedding,
                                        "match_threshold": -1.0,
                                        "match_count": fetch_count,
                                        "match_project_id": chunk_project_id,
                                    })
                                    for m in (matches if isinstance(matches, list) else []):
                                        if m.get("document_id") == doc_id and m.get("id") not in existing_result_ids:
                                            m["source"] = "chat_upload"
                                            all_results.append(m)
                                            existing_result_ids.add(m.get("id"))
                except Exception:
                    pass

        if all_results:
            doc_ids = list(set(r.get("document_id") for r in all_results if r.get("document_id")))
            if doc_ids:
                try:
                    docs = _supabase_query("documents", {
                        "select": "id,name",
                        "id": f"in.({','.join(doc_ids)})",
                    })
                    doc_names = {d["id"]: d["name"] for d in docs}
                    for r in all_results:
                        r["document_name"] = doc_names.get(r.get("document_id"), "Unknown")
                except Exception:
                    pass

        if not all_results:
            available = _list_available_document_names(project_id) if project_id else []
            return json.dumps({
                "message": "No relevant documents found for your query. "
                           "Try a different query, or use document_name to filter by filename. "
                           "Available documents listed below.",
                "query": query,
                "project_id": project_id,
                "available_documents": available,
                "tip": "Use document_name='<partial filename>' to restrict search to a specific file. "
                       "Do NOT pass a filename as the query parameter.",
            })

        seen_ids = set()
        unique_results = []
        for r in all_results:
            rid = r.get("id")
            if rid not in seen_ids:
                seen_ids.add(rid)
                unique_results.append(r)

        unique_results = unique_results[:max_results]

        output = {
            "query": query,
            "results_count": len(unique_results),
            "results": [
                {
                    "content": r.get("content", ""),
                    "document_name": r.get("document_name", "Unknown"),
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in unique_results
            ],
        }
        if document_name:
            output["document_name_filter"] = document_name
        if autocorrected:
            output["note"] = (
                "Query looked like a filename, so it was auto-redirected to a document_name "
                "filter. Next time, pass the filename via document_name= and use query= for "
                "the content you're looking for."
            )
        return json.dumps(output, indent=2, default=str)

    except Exception as e:
        return json.dumps({"error": f"Search failed: {type(e).__name__}: {e}"})
