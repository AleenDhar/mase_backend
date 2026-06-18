"""mase_knowledge.py — MASE's OWN isolated knowledge store (RAG).

COMPLETELY SEPARATE from VIBE's projects/documents tables. MASE knowledge lives in
its own tables (RLS-locked to the service role, so VIBE and the public anon key can't
see them):
  - public.mase_documents        (id, name, doc_type, created_at)
  - public.mase_document_chunks  (id, document_id, content, doc_type, chunk_index, embedding)
searched via the public.match_mase_document_chunks(query_embedding, match_count,
match_threshold) RPC. There is NO project_id / projects FK — this is a single MASE
knowledge namespace, NOT a VIBE "project", so nothing here appears in the VIBE UI.

Embeddings use OpenAI text-embedding-ada-002 (1536-dim, same as the rest of the RAG).
Callers pass the service-role Supabase client (server.supabase).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx

EMBED_MODEL = "text-embedding-ada-002"
_CHUNK = 1000
_OVERLAP = 200
_T_DOCS = "mase_documents"
_T_CHUNKS = "mase_document_chunks"


def _chunk_text(content: str) -> list[str]:
    out: list[str] = []
    start = 0
    n = len(content)
    while start < n:
        seg = content[start:start + _CHUNK]
        if seg.strip():
            out.append(seg)
        start += _CHUNK - _OVERLAP
    return out


async def _embed(texts: list[str]) -> list[list[float]]:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OpenAI API key not configured (needed for embeddings)")
    out: list[list[float]] = []
    for i in range(0, len(texts), 50):
        batch = texts[i:i + 50]
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": EMBED_MODEL, "input": batch},
            )
            r.raise_for_status()
            out.extend(item["embedding"] for item in r.json()["data"])
    return out


async def upload(supabase, *, name: str, content: str, doc_type: str | None = None) -> dict:
    """Chunk + embed `content` and store it in the MASE knowledge tables. Returns
    {document_id, name, chunks}."""
    name = (name or "").strip() or "Untitled"
    if not (content or "").strip():
        raise ValueError("content is required")
    loop = asyncio.get_event_loop()
    doc_id = str(uuid.uuid4())
    await loop.run_in_executor(None, lambda: supabase.table(_T_DOCS).insert(
        {"id": doc_id, "name": name, "doc_type": doc_type}).execute())
    chunks = _chunk_text(content)
    embeddings = await _embed(chunks)
    rows = [
        {"id": str(uuid.uuid4()), "document_id": doc_id, "content": c,
         "doc_type": doc_type, "chunk_index": i, "embedding": e}
        for i, (c, e) in enumerate(zip(chunks, embeddings))
    ]
    for i in range(0, len(rows), 25):
        b = rows[i:i + 25]
        await loop.run_in_executor(None, lambda bb=b: supabase.table(_T_CHUNKS).insert(bb).execute())
    return {"document_id": doc_id, "name": name, "chunks": len(rows)}


async def list_docs(supabase) -> list[dict]:
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, lambda: supabase.table(_T_DOCS)
                                     .select("id,name,doc_type,created_at")
                                     .order("created_at", desc=True).limit(500).execute())
    return res.data or []


async def get_doc(supabase, doc_id: str) -> dict | None:
    """Return one doc's metadata + its full text, reconstructed from the ordered chunks
    (we store chunks, not the raw doc, so drop the per-chunk overlap on rejoin)."""
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(None, lambda: supabase.table(_T_DOCS)
                                      .select("id,name,doc_type,created_at").eq("id", doc_id).limit(1).execute())
    rows = meta.data or []
    if not rows:
        return None
    doc = rows[0]
    ch = await loop.run_in_executor(None, lambda: supabase.table(_T_CHUNKS)
                                    .select("content,chunk_index").eq("document_id", doc_id)
                                    .order("chunk_index").execute())
    chunks = ch.data or []
    if chunks:
        content = chunks[0].get("content") or ""
        for c in chunks[1:]:
            content += (c.get("content") or "")[_OVERLAP:]
    else:
        content = ""
    doc["content"] = content
    doc["chunks"] = len(chunks)
    return doc


async def delete_doc(supabase, doc_id: str) -> None:
    loop = asyncio.get_event_loop()
    # chunks cascade on document delete, but delete explicitly too (belt and braces).
    await loop.run_in_executor(None, lambda: supabase.table(_T_CHUNKS).delete().eq("document_id", doc_id).execute())
    await loop.run_in_executor(None, lambda: supabase.table(_T_DOCS).delete().eq("id", doc_id).execute())
