"""mase_chat_docs.py — agent-authored downloadable documents for Ask Mase.

The chat agent calls its `create_document` tool (deal_engine_chat_agent.py) with a
title + markdown body; this module writes the .md to S3 under `chatdocs/` (the
knowledge bucket — NOT `uploads/`, which carries a 1-day expiry; `chatdocs/` has its
own 90-day lifecycle) and records a row in `public.mase_chat_documents`
(migrations/0016_mase_chat_documents.sql, RLS service-role only like mase_skills).

The user downloads via GET /api/deal-engine/documents/{doc_id} (server.py), which
streams the object back with Content-Disposition: attachment. We deliberately do NOT
hand out presigned S3 GET URLs: they are signed with the ECS task role's ROTATING
credentials, so they die within hours — a chat link must keep working days later.
The backend endpoint keeps the link stable and same-origin (auth at the frontend
proxy), and the doc_id is an unguessable uuid.

Uses analysis_store's service-role REST helpers (same pattern as mase_skills) and
its own boto3 client pinned to the REGIONAL endpoint + SigV4 (the global-endpoint
307-redirect trap — see server.py _get_s3)."""
from __future__ import annotations

import os
import re
import uuid

import analysis_store as store

T_DOCS = "mase_chat_documents"
_BUCKET = os.getenv("MASE_KNOWLEDGE_S3_BUCKET", "mase-knowledge-uploads-022187637784")
_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
_PREFIX = "chatdocs"
_MAX_DOC_CHARS = int(os.getenv("MASE_CHAT_DOC_MAX_CHARS", "400000"))  # ~400 KB of markdown

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client(
            "s3", region_name=_REGION,
            endpoint_url=f"https://s3.{_REGION}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def safe_filename(title: str) -> str:
    """Human-readable, header-safe download filename from a free-text title."""
    base = re.sub(r"[^A-Za-z0-9 ._-]", "", (title or "").strip()).strip(" .")
    base = re.sub(r"\s+", " ", base)[:80] or "document"
    return base + ".md"


def create(*, title: str, content: str, chat_id: str | None = None,
           opp_id: str | None = None) -> dict:
    """Write the markdown to S3 + record the row. Returns {doc_id, title, filename,
    size}. Raises on failure (the tool surfaces the error to the agent)."""
    title = (title or "").strip()[:200] or "Untitled document"
    content = content or ""
    if not content.strip():
        raise ValueError("document content is empty")
    if len(content) > _MAX_DOC_CHARS:
        raise ValueError(f"document too large ({len(content)} chars; max {_MAX_DOC_CHARS})")
    doc_id = str(uuid.uuid4())
    fname = safe_filename(title)
    key = f"{_PREFIX}/{doc_id}/{fname}"
    _get_s3().put_object(
        Bucket=_BUCKET, Key=key, Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8")
    store._insert(T_DOCS, {
        "id": doc_id, "title": title, "filename": fname, "s3_key": key,
        "chat_id": (chat_id or None), "opp_id": ((opp_id or "").strip()[:15] or None),
        "size_bytes": len(content.encode("utf-8")),
    }, returning=False)
    return {"doc_id": doc_id, "title": title, "filename": fname,
            "size": len(content.encode("utf-8"))}


def get(doc_id: str) -> dict | None:
    """Metadata row for one doc, or None."""
    did = (doc_id or "").strip()
    if not did:
        return None
    return store._first(store._select(
        T_DOCS, select="id,title,filename,s3_key,size_bytes,created_at",
        filters=[f"id=eq.{did}"], limit=1))


def fetch_content(s3_key: str) -> bytes:
    obj = _get_s3().get_object(Bucket=_BUCKET, Key=s3_key)
    return obj["Body"].read()
