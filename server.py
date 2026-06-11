"""
Canvas MCP connector.

A small remote MCP server that wraps the Canvas LMS REST API so Claude can read
your courses, grades, upcoming assignments, missing work, and to-do list.

It exposes four read-only tools over Streamable HTTP transport, which is what
Claude's custom remote-connector feature speaks. The whole server is gated
behind a secret URL path so a stranger who guesses the host still can't reach
your Canvas data.

Environment variables (set these on your host, never hard-code them):
  CANVAS_BASE_URL   e.g. https://yourschool.instructure.com   (no trailing slash)
  CANVAS_API_TOKEN  your Canvas access token (Account > Settings > New Access Token)
  MCP_SECRET        a long random string; becomes part of the connector URL
  PORT              provided automatically by most hosts (Railway, Render, etc.)
"""

import os
import datetime as dt
from typing import Any

import httpx
from fastmcp import FastMCP

# --- Config -----------------------------------------------------------------

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_API_TOKEN = os.environ.get("CANVAS_API_TOKEN", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "change-me")
PORT = int(os.environ.get("PORT", "8000"))

if not CANVAS_BASE_URL or not CANVAS_API_TOKEN:
    # Fail loud at boot rather than returning confusing errors per tool call.
    raise RuntimeError(
        "CANVAS_BASE_URL and CANVAS_API_TOKEN must both be set as environment variables."
    )

HEADERS = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}

mcp = FastMCP("Canvas")


# --- Canvas API helpers -----------------------------------------------------

async def _get(path: str, params: dict | None = None, max_pages: int = 10) -> list[dict]:
    """GET a Canvas endpoint, following Link-header pagination up to max_pages."""
    url = f"{CANVAS_BASE_URL}/api/v1/{path.lstrip('/')}"
    params = dict(params or {})
    params.setdefault("per_page", 100)
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        for _ in range(max_pages):
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                out.extend(data)
            else:
                # Some endpoints return a single object.
                return [data]
            nxt = _next_link(resp.headers.get("link", ""))
            if not nxt:
                break
            url, params = nxt, None  # the next URL already carries its query
    return out


def _next_link(link_header: str) -> str | None:
    """Parse the RFC-5988 Link header Canvas uses for pagination."""
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        rel = section[1].strip()
        if rel == 'rel="next"':
            return url
    return None


def _fmt_due(due: str | None) -> str:
    """Turn an ISO due date into something human, with a day countdown."""
    if not due:
        return "no due date"
    try:
        when = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError:
        return due
    now = dt.datetime.now(dt.timezone.utc)
    days = (when.date() - now.date()).days
    stamp = when.strftime("%a %b %-d, %-I:%M %p")
    if days < 0:
        return f"{stamp} ({abs(days)}d overdue)"
    if days == 0:
        return f"{stamp} (today)"
    if days == 1:
        return f"{stamp} (tomorrow)"
    return f"{stamp} (in {days}d)"


# --- Tools ------------------------------------------------------------------

@mcp.tool
async def list_courses() -> list[dict]:
    """List the student's active courses with current grade (score and letter)."""
    courses = await _get(
        "courses",
        {"enrollment_state": "active", "include[]": "total_scores"},
    )
    result = []
    for c in courses:
        if not c.get("name"):
            continue
        score = grade = None
        for e in c.get("enrollments", []):
            if e.get("type") == "student":
                score = e.get("computed_current_score")
                grade = e.get("computed_current_grade")
                break
        result.append(
            {
                "course_id": c.get("id"),
                "name": c.get("name"),
                "current_score": score,
                "current_grade": grade,
            }
        )
    return result


@mcp.tool
async def upcoming_assignments(days: int = 14) -> list[dict]:
    """Upcoming assignments across all courses within the next `days` days,
    including whether each one has been submitted, is missing, or is graded."""
    start = dt.datetime.now(dt.timezone.utc).date().isoformat()
    end = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).date().isoformat()
    items = await _get("planner/items", {"start_date": start, "end_date": end})
    out = []
    for it in items:
        plannable = it.get("plannable", {}) or {}
        subs = it.get("submissions") or {}
        if not isinstance(subs, dict):
            subs = {}
        due = plannable.get("due_at") or it.get("plannable_date")
        status = "not submitted"
        if subs.get("submitted"):
            status = "submitted"
        if subs.get("missing"):
            status = "MISSING"
        if subs.get("graded"):
            status = "graded"
        out.append(
            {
                "title": plannable.get("title") or it.get("plannable_type"),
                "type": it.get("plannable_type"),
                "course_id": it.get("course_id"),
                "due": _fmt_due(due),
                "due_raw": due,
                "points": plannable.get("points_possible"),
                "status": status,
                "url": it.get("html_url"),
            }
        )
    out.sort(key=lambda x: x.get("due_raw") or "9999")
    return out


@mcp.tool
async def missing_submissions() -> list[dict]:
    """Assignments that are past due and still not turned in."""
    items = await _get(
        "users/self/missing_submissions",
        {"filter[]": "submittable", "include[]": "course"},
    )
    out = []
    for a in items:
        out.append(
            {
                "title": a.get("name"),
                "course_id": a.get("course_id"),
                "due": _fmt_due(a.get("due_at")),
                "points": a.get("points_possible"),
                "url": a.get("html_url"),
            }
        )
    return out


@mcp.tool
async def canvas_todo() -> list[dict]:
    """Canvas's own to-do list: assignments flagged as needing action."""
    items = await _get("users/self/todo")
    out = []
    for t in items:
        a = t.get("assignment") or {}
        out.append(
            {
                "title": a.get("name") or t.get("type"),
                "course_id": t.get("course_id"),
                "due": _fmt_due(a.get("due_at")),
                "url": t.get("html_url") or a.get("html_url"),
            }
        )
    return out


# --- Entrypoint -------------------------------------------------------------

if __name__ == "__main__":
    # Mount the MCP endpoint under the secret path. The full connector URL is
    # therefore: https://<your-host>/<MCP_SECRET>/mcp
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        path=f"/{MCP_SECRET}/mcp",
    )
