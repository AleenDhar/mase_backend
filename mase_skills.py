"""mase_skills.py — MASE Skills store for the RevOps chat agent.

A "skill" is a named, reusable PROCEDURE the chat agent can load and follow on
demand — the Anthropic "Skills" model (progressive disclosure):
  * a short `description` ("when to use") is ALWAYS shown to the agent as a
    lightweight index (see skills_prompt_block), and
  * the full `body` (Markdown instructions) is pulled by the agent via the
    load_skill(name) tool ONLY when a request matches.

This is DISTINCT from the knowledge base (mase_knowledge / mase_documents): the
knowledge base is reference DATA retrieved by vector similarity; a skill is an
INSTRUCTION the agent executes.

Stored in one Supabase table, RLS-locked to the service role (mirrors the
mase_documents isolation). Uses analysis_store's service-role REST helpers (same
pattern as agent_prompt_store) so the load_skill tool can read it without a
Supabase client threaded through. Table created by migrations/0014_mase_skills.sql:

  public.mase_skills (id uuid PK, name text UNIQUE, description text, body text,
                      enabled bool default true, source_filename text,
                      created_at timestamptz, updated_at timestamptz)

Admin CRUD is exposed under /api/deal-engine/skills/* in server.py (admin-gated at
the Vercel proxy, same as /knowledge). The two hot-path readers used by the live
chat — skills_prompt_block() and get_by_name() — NEVER raise (a missing table or
REST blip degrades to "no skills"), so the chat is never blocked by this store.
"""
from __future__ import annotations

import re
import urllib.parse
import uuid

import analysis_store as store

T_SKILLS = "mase_skills"
_LIST_COLS = "id,name,description,enabled,source_filename,created_at,updated_at"


def _q(value: str) -> str:
    """URL-encode a PostgREST eq value (skill names are free text — spaces etc.)."""
    return urllib.parse.quote((value or ""), safe="")


def _strip_nul(s: str) -> str:
    """Postgres text can't hold NUL — a stray \\u0000 makes the insert fail with
    22P05 'unsupported Unicode escape sequence'. Strip them everywhere, always."""
    return (s or "").replace("\x00", "")


def _decode(raw: bytes) -> str:
    """Bytes -> text, tolerating UTF-16 (a common Windows save) and mojibake."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return _strip_nul(raw.decode("utf-16"))
        except Exception:  # noqa: BLE001
            pass
    try:
        return _strip_nul(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _strip_nul(raw.decode("utf-8", errors="replace"))


def _parse_front(front: str) -> dict:
    """Frontmatter -> {lowercased key: value}. Uses PyYAML so real Anthropic
    SKILL.md frontmatter parses correctly — notably the folded scalar form
    (`description: >-` + indented continuation lines), which a naive
    `key: value` split would read as the literal string '>-'. Falls back to a
    simple split if PyYAML is unavailable or the block isn't valid YAML."""
    try:
        import yaml  # PyYAML is pinned in requirements.txt
        data = yaml.safe_load(front)
        if isinstance(data, dict):
            return {str(k).strip().lower(): v for k, v in data.items()}
    except Exception:  # noqa: BLE001 — fall through to the simple parser
        pass
    out: dict = {}
    for line in front.split("\n"):
        if ":" not in line or line[:1] in (" ", "\t"):
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip().strip('"').strip("'")
    return out


def parse_skill_file(text: str, *, fallback_name: str = "") -> dict:
    """Parse skill TEXT (a .md/.skill markdown file, or pasted text) into
    {name, description, body}.

    Supports an OPTIONAL leading YAML frontmatter block delimited by '---':
        ---
        name: RFP Response
        description: >-
          Use when the user asks how to respond to / submit an RFP.
        ---
        <the instructions...>
    Only `name` and `description` are read. With no frontmatter, `name` falls back
    to the first Markdown '# heading' then `fallback_name`; `description` to the
    first non-empty, non-heading line.
    """
    text = _strip_nul((text or "").replace("\r\n", "\n").replace("\r", "\n"))
    name = ""
    description = ""
    body = text.strip()
    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", body, re.DOTALL)
    if m:
        front, body = m.group(1), m.group(2).strip()
        meta = _parse_front(front)
        name = str(meta.get("name") or "").strip()
        # Collapse folded/multi-line descriptions to one line for the prompt index.
        description = " ".join(str(meta.get("description") or "").split()).strip()
    if not name:
        hm = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        name = (hm.group(1).strip() if hm else "") or (fallback_name or "").strip() or "Untitled skill"
    if not description:
        for line in body.split("\n"):
            s = line.strip()
            if s and not s.startswith("#"):
                description = s[:300]
                break
    return {"name": name.strip()[:200], "description": description.strip()[:2000], "body": body}


def parse_skill_bundle(raw: bytes, *, filename: str = "") -> dict:
    """Parse an UPLOADED skill file (bytes) into {name, description, body}.

    Handles both shapes an admin can upload:
      * a **.skill ZIP bundle** — the real Anthropic Skills layout
        (`<dir>/SKILL.md` + optional `references/*.md`). SKILL.md drives
        name/description/body, and every OTHER text file in the bundle is
        APPENDED to the body as a labelled section. That matters because
        SKILL.md routinely instructs the agent to "read references/lessons.md";
        load_skill returns ONE blob, so the references must travel with it or
        those instructions dangle.
      * a plain **.md / .skill / .txt** text file — parsed directly.

    Raises ValueError with a readable message on an unusable bundle.
    """
    fallback = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if raw[:2] != b"PK":  # not a zip -> plain text
        return parse_skill_file(_decode(raw), fallback_name=fallback)

    import io
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"'{filename}' looks like a ZIP but could not be opened: {e}")
    names = [n for n in zf.namelist() if not n.endswith("/")]
    # Prefer the shallowest SKILL.md; else the shallowest markdown file.
    cands = sorted([n for n in names if n.rsplit("/", 1)[-1].lower() == "skill.md"],
                   key=lambda n: (n.count("/"), len(n)))
    if not cands:
        cands = sorted([n for n in names if n.lower().endswith((".md", ".markdown"))],
                       key=lambda n: (n.count("/"), len(n)))
    if not cands:
        raise ValueError(
            f"'{filename}' is a ZIP but contains no SKILL.md (or any .md). "
            f"Entries: {', '.join(names[:10]) or '(empty)'}")
    main = cands[0]
    parsed = parse_skill_file(_decode(zf.read(main)), fallback_name=fallback)
    root = main.rsplit("/", 1)[0] if "/" in main else ""
    extras = []
    for n in sorted(names):
        if n == main or not n.lower().endswith((".md", ".markdown", ".txt")):
            continue
        rel = n[len(root) + 1:] if root and n.startswith(root + "/") else n
        extras.append(f"\n\n---\n\n## Bundled reference: {rel}\n\n{_decode(zf.read(n))}")
    if extras:
        parsed["body"] = parsed["body"] + "".join(extras)
    return parsed


# ---------- admin CRUD (may raise; endpoints surface the error) ----------

def create(*, name: str, description: str, body: str,
           source_filename: str | None = None, enabled: bool = True) -> dict:
    """Create or, if a skill with the same `name` already exists, UPDATE it.
    Returns {id, name, updated}. `name` is UNIQUE — re-uploading a skill with the
    same name replaces it (id preserved by the on_conflict=name upsert)."""
    # Final NUL guard at the store boundary: Postgres text rejects  with
    # 22P05, which is how a ZIP read as text used to blow up the insert.
    name = _strip_nul(name or "").strip()
    description = _strip_nul(description or "").strip()
    body = _strip_nul(body or "")
    if not name:
        raise ValueError("skill name is required")
    if not body.strip():
        raise ValueError("skill body/instructions are required")
    existed = bool(store._first(store._select(
        T_SKILLS, select="id", filters=[f"name=eq.{_q(name)}"], limit=1)))
    row = {
        "name": name,
        "description": description,
        "body": body,
        "source_filename": source_filename,
        "enabled": bool(enabled),
        "updated_at": store._now(),
    }
    # id/created_at use their column defaults on INSERT; on conflict(name) only the
    # provided columns are updated (id + created_at preserved).
    res = store._upsert(T_SKILLS, row, on_conflict="name", returning=True)
    out = store._first(res) or {}
    return {"id": out.get("id"), "name": name, "updated": existed}


def list_skills() -> list[dict]:
    """Every skill (metadata only, no body) for the admin list, newest first."""
    return store._select(T_SKILLS, select=_LIST_COLS,
                         order="created_at.desc", limit=500) or []


def get(skill_id: str) -> dict | None:
    """One skill WITH its full body (admin viewer)."""
    return store._first(store._select(
        T_SKILLS, select="*", filters=[f"id=eq.{_q(skill_id)}"], limit=1))


def set_enabled(skill_id: str, enabled: bool) -> None:
    store._patch(T_SKILLS, {"id": skill_id},
                 {"enabled": bool(enabled), "updated_at": store._now()},
                 returning=False)


def delete(skill_id: str) -> None:
    store._delete(T_SKILLS, {"id": skill_id})


# ---------- chat hot path (NEVER raises — degrades to "no skills") ----------

def get_by_name(name: str) -> dict | None:
    """The ENABLED skill with this exact name (for the load_skill tool). Returns
    None on miss or any error — never raises into the chat."""
    try:
        return store._first(store._select(
            T_SKILLS, select="id,name,description,body,enabled",
            filters=[f"name=eq.{_q((name or '').strip())}", "enabled=eq.true"], limit=1))
    except Exception:  # noqa: BLE001 — chat must never break on this read
        return None


def enabled_index() -> list[dict]:
    """[{name, description}] for every enabled skill — the lightweight prompt
    index. Returns [] on any error."""
    try:
        rows = store._select(T_SKILLS, select="name,description",
                             filters=["enabled=eq.true"], order="name.asc") or []
        return [{"name": r.get("name"), "description": r.get("description") or ""}
                for r in rows if r.get("name")]
    except Exception:  # noqa: BLE001
        return []


def enabled_names() -> list[str]:
    return [s["name"] for s in enabled_index()]


def skills_prompt_block() -> str:
    """The 'SKILLS AVAILABLE' block appended to the chat system prompt. Empty
    string when there are no enabled skills (so the prompt is unchanged)."""
    idx = enabled_index()
    if not idx:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description']}" for s in idx)
    return (
        "\nSKILLS AVAILABLE — reusable, admin-authored procedures you can load on "
        "demand. When a user's request matches a skill's description below, call "
        'load_skill("<exact name>") to get its full instructions, then FOLLOW them '
        "for that reply. Do not guess a skill's contents from its name — load it:\n"
        + lines + "\n"
    )
