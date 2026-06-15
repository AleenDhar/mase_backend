"""
WordPress MCP Server
====================
Exposes a single WordPress site to the agent through the WordPress core REST
API (`/wp-json/wp/v2`), authenticated with an **Application Password** over
HTTP Basic auth (HTTPS). Application Passwords are built into WordPress 5.6+,
are per-user / revocable, and require no SSH access — the right fit for letting
the agent manage a (staging) site without sharing server credentials.

Credentials are read from the environment (injected via mcp_config.json):
  - WORDPRESS_URL           e.g. https://staging.example.com
  - WORDPRESS_USERNAME      a dedicated, least-privilege WordPress user
  - WORDPRESS_APP_PASSWORD  an Application Password for that user

Scope is full admin, limited to what the WordPress core `wp/v2` REST API
exposes: content (posts/pages), taxonomies (categories/tags), comments, media,
users, plugin lifecycle, and core site settings. This is NOT a code-deployment
channel (no theme/plugin PHP file deploys — that needs SSH/SFTP/Git/WP-CLI).

Tools (all prefixed `wordpress_`):
  Discovery: whoami, list_routes
  Posts:     list_posts, get_post, create_post, update_post, delete_post
  Pages:     list_pages, get_page, create_page, update_page, delete_page
  Taxonomy:  list_categories, create_category, update_category, delete_category,
             list_tags, create_tag, update_tag, delete_tag
  Comments:  list_comments, get_comment, moderate_comment, delete_comment
  Media:     list_media, get_media, upload_media_from_url,
             upload_media_from_base64, delete_media
  Users:     list_users, get_user, create_user, update_user, delete_user
  Plugins:   list_plugins, get_plugin, activate_plugin, deactivate_plugin,
             install_plugin, delete_plugin
  Settings:  get_settings, update_settings

Every tool returns structured JSON. On failure it returns a structured
{"error": ..., "status": <http>, "detail": <wordpress error body>} object
rather than failing silently.
"""

import base64
import json
import mimetypes
import os
from typing import Any, Optional
from urllib.parse import urlparse, unquote

import httpx
from fastmcp import FastMCP

WORDPRESS_URL = (os.environ.get("WORDPRESS_URL", "") or "").strip().rstrip("/")
WORDPRESS_USERNAME = (os.environ.get("WORDPRESS_USERNAME", "") or "").strip()
WORDPRESS_APP_PASSWORD = os.environ.get("WORDPRESS_APP_PASSWORD", "") or ""

REQUEST_TIMEOUT = float(os.environ.get("WORDPRESS_TIMEOUT_S", "30"))

mcp = FastMCP("wordpress-mcp-server")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _missing_credentials() -> Optional[dict]:
    """Return a structured error if any required credential is unset, else None."""
    missing = [
        name for name, val in (
            ("WORDPRESS_URL", WORDPRESS_URL),
            ("WORDPRESS_USERNAME", WORDPRESS_USERNAME),
            ("WORDPRESS_APP_PASSWORD", WORDPRESS_APP_PASSWORD),
        ) if not val
    ]
    if missing:
        return {
            "error": "WordPress credentials are not configured.",
            "missing": missing,
            "detail": (
                "Set WORDPRESS_URL, WORDPRESS_USERNAME, and WORDPRESS_APP_PASSWORD "
                "(an Application Password) in the environment."
            ),
        }
    return None


def _auth() -> tuple:
    """HTTP Basic auth tuple. WordPress strips whitespace from Application
    Passwords, so we send the value without spaces for reliability."""
    return (WORDPRESS_USERNAME, "".join(WORDPRESS_APP_PASSWORD.split()))


def _api_base() -> str:
    return f"{WORDPRESS_URL}/wp-json/wp/v2"


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_response(resp: httpx.Response, method: str, url: str):
    """Turn an httpx response into parsed JSON, or a structured error dict."""
    status = resp.status_code
    body_text = resp.text or ""
    parsed = None
    if body_text.strip():
        try:
            parsed = resp.json()
        except Exception:
            parsed = None
    if status >= 400:
        return {
            "error": f"WordPress API error {status} for {method} {url}",
            "status": status,
            "detail": parsed if parsed is not None else body_text[:1000],
        }
    if parsed is not None:
        return parsed
    if status == 204:
        return {"status": "ok", "status_code": status}
    return {"status_code": status, "detail": body_text[:1000]}


def _call(method: str, url: str, *, params=None, json_body=None,
          content=None, headers=None):
    """Core request. Returns (result_or_error, response_or_None)."""
    cred_err = _missing_credentials()
    if cred_err:
        return cred_err, None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = client.request(
                method, url,
                params=params,
                json=json_body,
                content=content,
                headers=req_headers,
                auth=_auth(),
            )
    except httpx.TimeoutException:
        return {"error": f"WordPress request timed out after {REQUEST_TIMEOUT:g}s",
                "method": method, "url": url}, None
    except httpx.HTTPError as e:
        return {"error": f"WordPress request failed: {e}", "method": method, "url": url}, None
    return _parse_response(resp, method, url), resp


def _request(method: str, path: str, **kwargs):
    """Request a wp/v2 path; return parsed body or structured error."""
    result, _ = _call(method, f"{_api_base()}{path}", **kwargs)
    return result


def _request_list(path: str, params: dict):
    """GET a wp/v2 collection; wrap items with pagination metadata from headers."""
    result, resp = _call("GET", f"{_api_base()}{path}", params=params)
    if isinstance(result, dict) and "error" in result:
        return result
    meta = {}
    if resp is not None:
        meta = {
            "total": _safe_int(resp.headers.get("X-WP-Total")),
            "total_pages": _safe_int(resp.headers.get("X-WP-TotalPages")),
        }
    return {
        **meta,
        "count": len(result) if isinstance(result, list) else None,
        "items": result,
    }


def _params(**kwargs) -> dict:
    """Drop None values so optional args don't override server defaults."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _force_param(force: bool) -> Optional[dict]:
    return {"force": "true"} if force else None


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_whoami() -> str:
    """
    Report the authenticated WordPress user and their capabilities/roles.

    Use this first to confirm credentials work and to see what the configured
    account is allowed to do (capabilities gate which other tools will succeed).

    Returns:
        JSON with the current user (id, name, slug, roles, capabilities).
    """
    return _dump(_request("GET", "/users/me", params={"context": "edit"}))


@mcp.tool()
def wordpress_list_routes() -> str:
    """
    List the site's available REST namespaces and route paths plus its
    authentication info, so the agent can self-orient about what the site
    exposes (e.g. WooCommerce or other plugin namespaces beyond wp/v2).

    Returns:
        JSON with name, description, url, namespaces, authentication, and the
        list of available route paths.
    """
    result, _ = _call("GET", f"{WORDPRESS_URL}/wp-json")
    if isinstance(result, dict) and "error" in result:
        return _dump(result)
    routes = result.get("routes", {}) if isinstance(result, dict) else {}
    summary = {
        "name": result.get("name") if isinstance(result, dict) else None,
        "description": result.get("description") if isinstance(result, dict) else None,
        "url": result.get("url") if isinstance(result, dict) else None,
        "namespaces": result.get("namespaces") if isinstance(result, dict) else None,
        "authentication": result.get("authentication") if isinstance(result, dict) else None,
        "routes": sorted(routes.keys()),
    }
    return _dump(summary)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_posts(
    search: Optional[str] = None,
    status: Optional[str] = None,
    per_page: int = 10,
    page: int = 1,
    categories: Optional[list] = None,
    tags: Optional[list] = None,
    author: Optional[int] = None,
    orderby: Optional[str] = None,
    order: Optional[str] = None,
) -> str:
    """
    List or search posts (newest first by default).

    Args:
        search: Free-text search across post content/title.
        status: Filter by status: "publish", "draft", "pending", "private",
            "future", "trash", or "any" to include every status. Default shows
            published only.
        per_page: Results per page (1-100, default 10).
        page: Page number for pagination (default 1).
        categories: List of category IDs to filter by.
        tags: List of tag IDs to filter by.
        author: Filter by author user ID.
        orderby: Sort field (e.g. "date", "title", "modified", "id").
        order: "asc" or "desc".

    Returns:
        JSON with total, total_pages, count, and items (post objects).
    """
    params = _params(
        search=search, status=status, per_page=per_page, page=page,
        categories=categories, tags=tags, author=author,
        orderby=orderby, order=order, context="edit",
    )
    return _dump(_request_list("/posts", params))


@mcp.tool()
def wordpress_get_post(post_id: int) -> str:
    """
    Get a single post by ID (full edit context, so drafts/private included).

    Args:
        post_id: The post ID.

    Returns:
        JSON with the post object.
    """
    return _dump(_request("GET", f"/posts/{post_id}", params={"context": "edit"}))


@mcp.tool()
def wordpress_create_post(
    title: Optional[str] = None,
    content: Optional[str] = None,
    status: str = "draft",
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
    categories: Optional[list] = None,
    tags: Optional[list] = None,
    author: Optional[int] = None,
    featured_media: Optional[int] = None,
    comment_status: Optional[str] = None,
    date: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Create a post. Defaults to "draft" status (safe).

    Args:
        title: Post title.
        content: Post body (HTML allowed).
        status: "draft", "publish", "pending", "private", or "future".
            Default "draft".
        excerpt: Optional excerpt/summary.
        slug: Optional URL slug.
        categories: List of category IDs.
        tags: List of tag IDs.
        author: Author user ID (defaults to the authenticated user).
        featured_media: Media (attachment) ID to set as the featured image.
        comment_status: "open" or "closed".
        date: Publish date (ISO 8601). Required/meaningful for status "future".
        extra: Any additional wp/v2 post fields (e.g. {"meta": {...}}).

    Returns:
        JSON with the created post object.
    """
    body = _params(
        title=title, content=content, status=status, excerpt=excerpt,
        slug=slug, categories=categories, tags=tags, author=author,
        featured_media=featured_media, comment_status=comment_status, date=date,
    )
    if extra:
        body.update(extra)
    return _dump(_request("POST", "/posts", json_body=body))


@mcp.tool()
def wordpress_update_post(
    post_id: int,
    title: Optional[str] = None,
    content: Optional[str] = None,
    status: Optional[str] = None,
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
    categories: Optional[list] = None,
    tags: Optional[list] = None,
    author: Optional[int] = None,
    featured_media: Optional[int] = None,
    comment_status: Optional[str] = None,
    date: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Update an existing post. Only provided fields are changed.

    Set status="publish" to publish a draft, or status="draft" to unpublish.

    Args:
        post_id: The post ID to update.
        (other args): Same as wordpress_create_post; omit to leave unchanged.
        extra: Any additional wp/v2 post fields to set.

    Returns:
        JSON with the updated post object.
    """
    body = _params(
        title=title, content=content, status=status, excerpt=excerpt,
        slug=slug, categories=categories, tags=tags, author=author,
        featured_media=featured_media, comment_status=comment_status, date=date,
    )
    if extra:
        body.update(extra)
    if not body:
        return _dump({"error": "No fields provided to update."})
    return _dump(_request("POST", f"/posts/{post_id}", json_body=body))


@mcp.tool()
def wordpress_delete_post(post_id: int, force: bool = False) -> str:
    """
    Delete a post. By default it is moved to Trash; set force=True to delete
    permanently.

    Args:
        post_id: The post ID.
        force: If True, bypass Trash and delete permanently. Default False.

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/posts/{post_id}", params=_force_param(force)))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_pages(
    search: Optional[str] = None,
    status: Optional[str] = None,
    per_page: int = 10,
    page: int = 1,
    parent: Optional[int] = None,
    orderby: Optional[str] = None,
    order: Optional[str] = None,
) -> str:
    """
    List or search pages.

    Args:
        search: Free-text search.
        status: "publish", "draft", "pending", "private", or "any".
        per_page: Results per page (1-100, default 10).
        page: Page number (default 1).
        parent: Filter by parent page ID.
        orderby: Sort field (e.g. "date", "title", "menu_order").
        order: "asc" or "desc".

    Returns:
        JSON with total, total_pages, count, and items (page objects).
    """
    params = _params(
        search=search, status=status, per_page=per_page, page=page,
        parent=parent, orderby=orderby, order=order, context="edit",
    )
    return _dump(_request_list("/pages", params))


@mcp.tool()
def wordpress_get_page(page_id: int) -> str:
    """
    Get a single page by ID (full edit context).

    Args:
        page_id: The page ID.

    Returns:
        JSON with the page object.
    """
    return _dump(_request("GET", f"/pages/{page_id}", params={"context": "edit"}))


@mcp.tool()
def wordpress_create_page(
    title: Optional[str] = None,
    content: Optional[str] = None,
    status: str = "draft",
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
    parent: Optional[int] = None,
    menu_order: Optional[int] = None,
    author: Optional[int] = None,
    featured_media: Optional[int] = None,
    comment_status: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Create a page. Defaults to "draft" status (safe).

    Args:
        title: Page title.
        content: Page body (HTML allowed).
        status: "draft", "publish", "pending", or "private". Default "draft".
        excerpt: Optional excerpt.
        slug: Optional URL slug.
        parent: Parent page ID (for hierarchy).
        menu_order: Ordering value among siblings.
        author: Author user ID.
        featured_media: Featured image media ID.
        comment_status: "open" or "closed".
        extra: Any additional wp/v2 page fields.

    Returns:
        JSON with the created page object.
    """
    body = _params(
        title=title, content=content, status=status, excerpt=excerpt, slug=slug,
        parent=parent, menu_order=menu_order, author=author,
        featured_media=featured_media, comment_status=comment_status,
    )
    if extra:
        body.update(extra)
    return _dump(_request("POST", "/pages", json_body=body))


@mcp.tool()
def wordpress_update_page(
    page_id: int,
    title: Optional[str] = None,
    content: Optional[str] = None,
    status: Optional[str] = None,
    excerpt: Optional[str] = None,
    slug: Optional[str] = None,
    parent: Optional[int] = None,
    menu_order: Optional[int] = None,
    author: Optional[int] = None,
    featured_media: Optional[int] = None,
    comment_status: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Update an existing page. Only provided fields are changed.

    Args:
        page_id: The page ID to update.
        (other args): Same as wordpress_create_page; omit to leave unchanged.

    Returns:
        JSON with the updated page object.
    """
    body = _params(
        title=title, content=content, status=status, excerpt=excerpt, slug=slug,
        parent=parent, menu_order=menu_order, author=author,
        featured_media=featured_media, comment_status=comment_status,
    )
    if extra:
        body.update(extra)
    if not body:
        return _dump({"error": "No fields provided to update."})
    return _dump(_request("POST", f"/pages/{page_id}", json_body=body))


@mcp.tool()
def wordpress_delete_page(page_id: int, force: bool = False) -> str:
    """
    Delete a page (Trash by default; force=True for permanent delete).

    Args:
        page_id: The page ID.
        force: If True, delete permanently. Default False.

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/pages/{page_id}", params=_force_param(force)))


# ---------------------------------------------------------------------------
# Taxonomies: Categories & Tags
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_categories(
    search: Optional[str] = None,
    per_page: int = 50,
    page: int = 1,
    post: Optional[int] = None,
    orderby: Optional[str] = None,
    order: Optional[str] = None,
) -> str:
    """
    List or search categories.

    Args:
        search: Free-text search on category name.
        per_page: Results per page (1-100, default 50).
        page: Page number (default 1).
        post: Limit to categories assigned to this post ID.
        orderby: Sort field (e.g. "name", "count", "id").
        order: "asc" or "desc".

    Returns:
        JSON with total, total_pages, count, and items (category objects).
    """
    params = _params(search=search, per_page=per_page, page=page, post=post,
                     orderby=orderby, order=order)
    return _dump(_request_list("/categories", params))


@mcp.tool()
def wordpress_create_category(
    name: str,
    description: Optional[str] = None,
    slug: Optional[str] = None,
    parent: Optional[int] = None,
) -> str:
    """
    Create a category.

    Args:
        name: Category name (required).
        description: Optional description.
        slug: Optional URL slug.
        parent: Optional parent category ID (for nesting).

    Returns:
        JSON with the created category object.
    """
    body = _params(name=name, description=description, slug=slug, parent=parent)
    return _dump(_request("POST", "/categories", json_body=body))


@mcp.tool()
def wordpress_update_category(
    category_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    slug: Optional[str] = None,
    parent: Optional[int] = None,
) -> str:
    """
    Update a category. Only provided fields change.

    Args:
        category_id: The category ID.
        name: New name.
        description: New description.
        slug: New slug.
        parent: New parent category ID.

    Returns:
        JSON with the updated category object.
    """
    body = _params(name=name, description=description, slug=slug, parent=parent)
    if not body:
        return _dump({"error": "No fields provided to update."})
    return _dump(_request("POST", f"/categories/{category_id}", json_body=body))


@mcp.tool()
def wordpress_delete_category(category_id: int) -> str:
    """
    Delete a category permanently (terms have no Trash). Posts in it are not
    deleted; they simply lose this category.

    Args:
        category_id: The category ID.

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/categories/{category_id}", params={"force": "true"}))


@mcp.tool()
def wordpress_list_tags(
    search: Optional[str] = None,
    per_page: int = 50,
    page: int = 1,
    post: Optional[int] = None,
    orderby: Optional[str] = None,
    order: Optional[str] = None,
) -> str:
    """
    List or search tags.

    Args:
        search: Free-text search on tag name.
        per_page: Results per page (1-100, default 50).
        page: Page number (default 1).
        post: Limit to tags assigned to this post ID.
        orderby: Sort field (e.g. "name", "count", "id").
        order: "asc" or "desc".

    Returns:
        JSON with total, total_pages, count, and items (tag objects).
    """
    params = _params(search=search, per_page=per_page, page=page, post=post,
                     orderby=orderby, order=order)
    return _dump(_request_list("/tags", params))


@mcp.tool()
def wordpress_create_tag(
    name: str,
    description: Optional[str] = None,
    slug: Optional[str] = None,
) -> str:
    """
    Create a tag.

    Args:
        name: Tag name (required).
        description: Optional description.
        slug: Optional URL slug.

    Returns:
        JSON with the created tag object.
    """
    body = _params(name=name, description=description, slug=slug)
    return _dump(_request("POST", "/tags", json_body=body))


@mcp.tool()
def wordpress_update_tag(
    tag_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    slug: Optional[str] = None,
) -> str:
    """
    Update a tag. Only provided fields change.

    Args:
        tag_id: The tag ID.
        name: New name.
        description: New description.
        slug: New slug.

    Returns:
        JSON with the updated tag object.
    """
    body = _params(name=name, description=description, slug=slug)
    if not body:
        return _dump({"error": "No fields provided to update."})
    return _dump(_request("POST", f"/tags/{tag_id}", json_body=body))


@mcp.tool()
def wordpress_delete_tag(tag_id: int) -> str:
    """
    Delete a tag permanently (terms have no Trash).

    Args:
        tag_id: The tag ID.

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/tags/{tag_id}", params={"force": "true"}))


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

_COMMENT_STATUS_MAP = {
    "approve": "approve", "approved": "approve", "1": "approve",
    "hold": "hold", "unapprove": "hold", "unapproved": "hold",
    "pending": "hold", "0": "hold",
    "spam": "spam",
    "trash": "trash",
}


@mcp.tool()
def wordpress_list_comments(
    post: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    per_page: int = 10,
    page: int = 1,
) -> str:
    """
    List or search comments (for moderation).

    Args:
        post: Limit to comments on this post ID.
        status: Filter by moderation status: "approve", "hold", "spam",
            "trash", or "all".
        search: Free-text search on comment content.
        per_page: Results per page (1-100, default 10).
        page: Page number (default 1).

    Returns:
        JSON with total, total_pages, count, and items (comment objects).
    """
    params = _params(post=post, status=status, search=search,
                     per_page=per_page, page=page, context="edit")
    return _dump(_request_list("/comments", params))


@mcp.tool()
def wordpress_get_comment(comment_id: int) -> str:
    """
    Get a single comment by ID.

    Args:
        comment_id: The comment ID.

    Returns:
        JSON with the comment object.
    """
    return _dump(_request("GET", f"/comments/{comment_id}", params={"context": "edit"}))


@mcp.tool()
def wordpress_moderate_comment(comment_id: int, status: str) -> str:
    """
    Moderate a comment by changing its status.

    Args:
        comment_id: The comment ID.
        status: One of "approve", "hold" (unapprove), "spam", or "trash".

    Returns:
        JSON with the updated comment object.
    """
    mapped = _COMMENT_STATUS_MAP.get(status.strip().lower())
    if not mapped:
        return _dump({
            "error": f"Invalid comment status '{status}'.",
            "detail": "Use one of: approve, hold, spam, trash.",
        })
    return _dump(_request("POST", f"/comments/{comment_id}", json_body={"status": mapped}))


@mcp.tool()
def wordpress_delete_comment(comment_id: int, force: bool = False) -> str:
    """
    Delete a comment (Trash by default; force=True for permanent delete).

    Args:
        comment_id: The comment ID.
        force: If True, delete permanently. Default False (moves to Trash).

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/comments/{comment_id}", params=_force_param(force)))


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_media(
    search: Optional[str] = None,
    media_type: Optional[str] = None,
    per_page: int = 10,
    page: int = 1,
    parent: Optional[int] = None,
) -> str:
    """
    List or search media library items.

    Args:
        search: Free-text search.
        media_type: Filter by type: "image", "video", "audio", "application",
            "text", or "file".
        per_page: Results per page (1-100, default 10).
        page: Page number (default 1).
        parent: Limit to media attached to this post ID.

    Returns:
        JSON with total, total_pages, count, and items (media objects).
    """
    params = _params(search=search, media_type=media_type, per_page=per_page,
                     page=page, parent=parent, context="edit")
    return _dump(_request_list("/media", params))


@mcp.tool()
def wordpress_get_media(media_id: int) -> str:
    """
    Get a single media item by ID.

    Args:
        media_id: The media (attachment) ID.

    Returns:
        JSON with the media object (including source_url).
    """
    return _dump(_request("GET", f"/media/{media_id}", params={"context": "edit"}))


def _upload_media(raw: bytes, filename: str, mime_type: str,
                  title: Optional[str], alt_text: Optional[str],
                  caption: Optional[str], description: Optional[str]):
    """Upload raw bytes to the media library, then patch metadata if given."""
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": mime_type or "application/octet-stream",
    }
    result, _ = _call("POST", f"{_api_base()}/media", content=raw, headers=headers)
    if isinstance(result, dict) and "error" in result:
        return result
    media_id = result.get("id") if isinstance(result, dict) else None
    meta = _params(title=title, alt_text=alt_text, caption=caption, description=description)
    if media_id and meta:
        patched = _request("POST", f"/media/{media_id}", json_body=meta)
        if isinstance(patched, dict) and "error" not in patched:
            return patched
    return result


@mcp.tool()
def wordpress_upload_media_from_url(
    url: str,
    filename: Optional[str] = None,
    title: Optional[str] = None,
    alt_text: Optional[str] = None,
    caption: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Download a file from a URL and upload it to the WordPress media library.

    Args:
        url: Public URL of the file to fetch and upload.
        filename: Optional filename to store it as (derived from the URL if omitted).
        title: Optional media title.
        alt_text: Optional alt text (for images).
        caption: Optional caption.
        description: Optional description.

    Returns:
        JSON with the created media object (including id and source_url).
    """
    cred_err = _missing_credentials()
    if cred_err:
        return _dump(cred_err)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            r = client.get(url)
    except httpx.HTTPError as e:
        return _dump({"error": f"Failed to download source URL: {e}", "url": url})
    if r.status_code >= 400:
        return _dump({"error": f"Failed to download source URL (HTTP {r.status_code})",
                      "url": url})
    raw = r.content
    if not filename:
        filename = unquote(os.path.basename(urlparse(url).path)) or "upload"
    mime_type = (r.headers.get("Content-Type", "").split(";")[0].strip()
                 or mimetypes.guess_type(filename)[0]
                 or "application/octet-stream")
    if "." not in filename:
        ext = mimetypes.guess_extension(mime_type) or ""
        filename = f"{filename}{ext}"
    return _dump(_upload_media(raw, filename, mime_type, title, alt_text, caption, description))


@mcp.tool()
def wordpress_upload_media_from_base64(
    base64_data: str,
    filename: str,
    mime_type: Optional[str] = None,
    title: Optional[str] = None,
    alt_text: Optional[str] = None,
    caption: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Upload raw file bytes (base64-encoded) to the WordPress media library.

    Args:
        base64_data: The file contents, base64-encoded.
        filename: Filename to store it as (its extension helps infer the type).
        mime_type: MIME type (e.g. "image/png"); inferred from filename if omitted.
        title: Optional media title.
        alt_text: Optional alt text (for images).
        caption: Optional caption.
        description: Optional description.

    Returns:
        JSON with the created media object (including id and source_url).
    """
    try:
        raw = base64.b64decode(base64_data, validate=False)
    except Exception as e:
        return _dump({"error": f"Invalid base64 data: {e}"})
    mt = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return _dump(_upload_media(raw, filename, mt, title, alt_text, caption, description))


@mcp.tool()
def wordpress_delete_media(media_id: int) -> str:
    """
    Delete a media item permanently (attachments cannot be trashed).

    Args:
        media_id: The media (attachment) ID.

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/media/{media_id}", params={"force": "true"}))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_users(
    search: Optional[str] = None,
    roles: Optional[str] = None,
    per_page: int = 10,
    page: int = 1,
    orderby: Optional[str] = None,
    order: Optional[str] = None,
) -> str:
    """
    List or search users.

    Args:
        search: Free-text search (name, email, etc.).
        roles: Comma-separated role slugs to filter by (e.g. "editor,author").
        per_page: Results per page (1-100, default 10).
        page: Page number (default 1).
        orderby: Sort field (e.g. "name", "registered_date", "id").
        order: "asc" or "desc".

    Returns:
        JSON with total, total_pages, count, and items (user objects).
    """
    params = _params(search=search, roles=roles, per_page=per_page, page=page,
                     orderby=orderby, order=order, context="edit")
    return _dump(_request_list("/users", params))


@mcp.tool()
def wordpress_get_user(user_id: int) -> str:
    """
    Get a single user by ID.

    Args:
        user_id: The user ID.

    Returns:
        JSON with the user object.
    """
    return _dump(_request("GET", f"/users/{user_id}", params={"context": "edit"}))


@mcp.tool()
def wordpress_create_user(
    username: str,
    email: str,
    password: str,
    roles: Optional[list] = None,
    name: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    url: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Create a user.

    Args:
        username: Login username (required).
        email: Email address (required).
        password: Initial password (required).
        roles: List of role slugs (e.g. ["editor"]). Defaults to the site's
            default role if omitted.
        name: Display name.
        first_name: First name.
        last_name: Last name.
        url: Website URL.
        description: Bio/description.

    Returns:
        JSON with the created user object.
    """
    body = _params(username=username, email=email, password=password, roles=roles,
                   name=name, first_name=first_name, last_name=last_name,
                   url=url, description=description)
    return _dump(_request("POST", "/users", json_body=body))


@mcp.tool()
def wordpress_update_user(
    user_id: int,
    email: Optional[str] = None,
    password: Optional[str] = None,
    roles: Optional[list] = None,
    name: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    url: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Update a user. Only provided fields change.

    Args:
        user_id: The user ID to update.
        email: New email.
        password: New password.
        roles: New list of role slugs.
        name: New display name.
        first_name: New first name.
        last_name: New last name.
        url: New website URL.
        description: New bio/description.

    Returns:
        JSON with the updated user object.
    """
    body = _params(email=email, password=password, roles=roles, name=name,
                   first_name=first_name, last_name=last_name, url=url,
                   description=description)
    if not body:
        return _dump({"error": "No fields provided to update."})
    return _dump(_request("POST", f"/users/{user_id}", json_body=body))


@mcp.tool()
def wordpress_delete_user(user_id: int, reassign: Optional[int] = None) -> str:
    """
    Delete a user. WordPress always deletes users permanently (no Trash).

    Args:
        user_id: The user ID to delete.
        reassign: User ID to reassign this user's posts/content to. WordPress
            requires this; if omitted, the user's content is deleted too.

    Returns:
        JSON confirming the deletion.
    """
    params = {"force": "true"}
    if reassign is not None:
        params["reassign"] = str(reassign)
    return _dump(_request("DELETE", f"/users/{user_id}", params=params))


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_list_plugins(
    search: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """
    List installed plugins.

    Args:
        search: Free-text search on plugin name/description.
        status: Filter by "active" or "inactive".

    Returns:
        JSON array of plugin objects. Each has a "plugin" field (e.g.
        "akismet/akismet") used to identify it in the other plugin tools.
    """
    params = _params(search=search, status=status)
    return _dump(_request("GET", "/plugins", params=params or None))


@mcp.tool()
def wordpress_get_plugin(plugin: str) -> str:
    """
    Get details for a single installed plugin.

    Args:
        plugin: The plugin path without ".php" (e.g. "akismet/akismet" or "hello").

    Returns:
        JSON with the plugin object.
    """
    return _dump(_request("GET", f"/plugins/{plugin}"))


@mcp.tool()
def wordpress_activate_plugin(plugin: str) -> str:
    """
    Activate an installed plugin.

    Args:
        plugin: The plugin path without ".php" (e.g. "akismet/akismet").

    Returns:
        JSON with the updated plugin object.
    """
    return _dump(_request("POST", f"/plugins/{plugin}", json_body={"status": "active"}))


@mcp.tool()
def wordpress_deactivate_plugin(plugin: str) -> str:
    """
    Deactivate an active plugin.

    Args:
        plugin: The plugin path without ".php" (e.g. "akismet/akismet").

    Returns:
        JSON with the updated plugin object.
    """
    return _dump(_request("POST", f"/plugins/{plugin}", json_body={"status": "inactive"}))


@mcp.tool()
def wordpress_install_plugin(slug: str, activate: bool = False) -> str:
    """
    Install a plugin from the WordPress.org plugin directory by its slug.

    Args:
        slug: The WordPress.org plugin slug (e.g. "classic-editor", "wordpress-seo").
        activate: If True, activate it immediately after install. Default False.

    Returns:
        JSON with the installed plugin object.
    """
    body = {"slug": slug, "status": "active" if activate else "inactive"}
    return _dump(_request("POST", "/plugins", json_body=body))


@mcp.tool()
def wordpress_delete_plugin(plugin: str) -> str:
    """
    Delete (uninstall) an installed plugin. It must be inactive first — deactivate
    it before deleting, or this returns an error.

    Args:
        plugin: The plugin path without ".php" (e.g. "akismet/akismet").

    Returns:
        JSON confirming the deletion.
    """
    return _dump(_request("DELETE", f"/plugins/{plugin}"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@mcp.tool()
def wordpress_get_settings() -> str:
    """
    Read the site's core settings exposed by /wp/v2/settings (e.g. title,
    description/tagline, timezone, date/time formats, posts-per-page, default
    category, front-page settings).

    Returns:
        JSON with the settings object.
    """
    return _dump(_request("GET", "/settings"))


@mcp.tool()
def wordpress_update_settings(
    title: Optional[str] = None,
    description: Optional[str] = None,
    timezone: Optional[str] = None,
    date_format: Optional[str] = None,
    time_format: Optional[str] = None,
    start_of_week: Optional[int] = None,
    language: Optional[str] = None,
    use_smilies: Optional[bool] = None,
    default_category: Optional[int] = None,
    default_comment_status: Optional[str] = None,
    default_ping_status: Optional[str] = None,
    default_post_format: Optional[str] = None,
    posts_per_page: Optional[int] = None,
    show_on_front: Optional[str] = None,
    page_on_front: Optional[int] = None,
    page_for_posts: Optional[int] = None,
    email: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Update core site settings. Only provided fields change.

    Args:
        title: Site title.
        description: Tagline.
        timezone: Timezone string (e.g. "America/New_York").
        date_format / time_format: PHP date/time format strings.
        start_of_week: 0 (Sunday) - 6 (Saturday).
        language: Site language code (e.g. "en_US").
        use_smilies: Convert emoticons to graphics.
        default_category: Default category ID for new posts.
        default_comment_status: "open" or "closed".
        default_ping_status: "open" or "closed".
        default_post_format: e.g. "standard", "aside", "gallery".
        posts_per_page: Blog posts shown per page.
        show_on_front: "posts" or "page".
        page_on_front: Front page's page ID (when show_on_front="page").
        page_for_posts: Posts page's page ID (when show_on_front="page").
        email: Site admin email.
        extra: Any other /wp/v2/settings fields not listed above.

    Returns:
        JSON with the updated settings object.
    """
    body = _params(
        title=title, description=description, timezone=timezone,
        date_format=date_format, time_format=time_format,
        start_of_week=start_of_week, language=language, use_smilies=use_smilies,
        default_category=default_category,
        default_comment_status=default_comment_status,
        default_ping_status=default_ping_status,
        default_post_format=default_post_format, posts_per_page=posts_per_page,
        show_on_front=show_on_front, page_on_front=page_on_front,
        page_for_posts=page_for_posts, email=email,
    )
    if extra:
        body.update(extra)
    if not body:
        return _dump({"error": "No settings provided to update."})
    return _dump(_request("POST", "/settings", json_body=body))


if __name__ == "__main__":
    mcp.run()
