"""Facebook Pages and the Instagram professional accounts linked to them."""

from __future__ import annotations

import sys

from reelpost.errors import GraphAPIError, ReelpostError
from reelpost.graph import GraphClient


def list_pages(client: GraphClient) -> list[dict]:
    try:
        rows = client.collect(
            "/me/accounts",
            {"fields": "id,name,access_token,tasks,instagram_business_account{id,username}"},
        )
    except GraphAPIError as exc:
        raise ReelpostError(f"Could not list Pages: {exc}") from exc
    pages = []
    for row in rows:
        ig = row.get("instagram_business_account") or {}
        pages.append(
            {
                "id": str(row.get("id") or ""),
                "name": row.get("name") or "",
                "access_token": row.get("access_token") or "",
                "tasks": list(row.get("tasks") or []),
                "ig_user_id": str(ig["id"]) if ig.get("id") else None,
                "ig_username": ig.get("username"),
            }
        )
    return [page for page in pages if page["id"]]


def enrich_instagram(client: GraphClient, page: dict) -> dict:
    if page.get("ig_user_id"):
        return page
    try:
        body = client.get(
            f"/{page['id']}",
            {"fields": "name,instagram_business_account{id,username}"},
        )
    except GraphAPIError as exc:
        raise ReelpostError(f"Could not read the Instagram account linked to the Page: {exc}") from exc
    ig = body.get("instagram_business_account") or {}
    if body.get("name") and not page.get("name"):
        page["name"] = body["name"]
    page["ig_user_id"] = str(ig["id"]) if ig.get("id") else None
    page["ig_username"] = ig.get("username")
    return page


def choose_page(pages: list[dict], query: str | None, *, input_fn=input, interactive: bool = True) -> dict:
    if not pages:
        raise ReelpostError(
            "No Facebook Pages were returned. The login needs pages_show_list, "
            "and this user needs a role on the Page."
        )
    if query:
        needle = query.casefold()
        matches = [page for page in pages if page["id"] == query or (page.get("name") or "").casefold() == needle]
        if not matches:
            available = ", ".join(_label(page) for page in pages)
            raise ReelpostError(f"No Page matched {query!r}. Available: {available}")
        pages = matches
    if len(pages) == 1:
        return pages[0]
    if not interactive:
        available = ", ".join(_label(page) for page in pages)
        raise ReelpostError(f"Several Pages are available. Pass --page. {available}")
    for index, page in enumerate(pages, start=1):
        print(f"{index}. {_label(page)}", file=sys.stderr)
    raw = input_fn("Choose a Page number: ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(pages):
        raise ReelpostError(f"Choose a number from 1 to {len(pages)}.")
    return pages[int(raw) - 1]


def public_page(page: dict) -> dict:
    """Drop the Page token before anything is printed."""
    return {
        "id": page.get("id"),
        "name": page.get("name"),
        "instagram_id": page.get("ig_user_id"),
        "instagram_username": page.get("ig_username"),
        "tasks": page.get("tasks") or [],
    }


def warn_tasks(page: dict) -> str | None:
    tasks = page.get("tasks") or []
    if tasks and not {"CREATE_CONTENT", "MANAGE"}.intersection(tasks):
        return (
            "This user does not have CREATE_CONTENT or MANAGE on the Page. "
            "Publishing can fail until a Page admin grants one of those tasks."
        )
    return None


def _label(page: dict) -> str:
    ig = f" @{page['ig_username']}" if page.get("ig_username") else ""
    return f"{page.get('name') or 'Untitled'} ({page['id']}){ig}"
