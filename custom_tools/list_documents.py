import json
import os
import sys
from typing import Any, List

from langchain_core.tools import tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rag_context import current_project_id as _current_project_id, current_chat_id as _current_chat_id_for_rag

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


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


@tool
def list_documents() -> str:
    """List all documents indexed in the current project's knowledge base.

    Use this tool to verify which files are available before searching them.
    Returns each document's name, ID, and upload date.

    This is the correct tool for pre-flight file verification — do NOT use
    search_knowledge with a filename as the query to check if a file exists.

    Returns:
        JSON string with the list of indexed documents and their metadata.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return json.dumps({"error": "Supabase not configured."})

    project_id = _current_project_id.get(None)
    chat_id = _current_chat_id_for_rag.get(None)

    if not project_id and not chat_id:
        return json.dumps({"error": "No project context available."})

    try:
        all_docs: List[dict] = []
        seen_ids: set = set()

        if project_id:
            docs = _supabase_query("documents", {
                "select": "id,name,created_at",
                "project_id": f"eq.{project_id}",
                "order": "created_at.asc",
            })
            for d in (docs if isinstance(docs, list) else []):
                if d.get("id") not in seen_ids:
                    seen_ids.add(d["id"])
                    all_docs.append(d)

        if chat_id:
            try:
                chat_doc_refs = _supabase_query("chat_documents", {
                    "select": "document_id",
                    "chat_id": f"eq.{chat_id}",
                })
                chat_doc_ids = [d["document_id"] for d in chat_doc_refs if d.get("document_id")]
                if chat_doc_ids:
                    extra_docs = _supabase_query("documents", {
                        "select": "id,name,created_at",
                        "id": f"in.({','.join(chat_doc_ids)})",
                    })
                    for d in (extra_docs if isinstance(extra_docs, list) else []):
                        if d.get("id") not in seen_ids:
                            seen_ids.add(d["id"])
                            all_docs.append(d)
            except Exception:
                pass

        if not all_docs:
            return json.dumps({
                "message": "No documents are indexed in this project yet.",
                "document_count": 0,
                "documents": [],
            })

        return json.dumps({
            "document_count": len(all_docs),
            "documents": [
                {
                    "name": d.get("name", "Unknown"),
                    "id": d.get("id"),
                    "uploaded_at": d.get("created_at", ""),
                }
                for d in all_docs
            ],
        }, indent=2, default=str)

    except Exception as e:
        return json.dumps({"error": f"list_documents failed: {type(e).__name__}: {e}"})
