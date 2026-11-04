"""In-app developer documentation for the Orbis API — `/developers/api/`.

Renders the Markdown in `docs/` at request time, so the repo files stay the
single source: edit `docs/ORBIS_API_GUIDE.md` (or the two references) and
the page follows on the next deploy. Open to any signed-in user — the docs
hold no secrets; tokens are issued separately from the admin.
docs/DIRECTORY_API_PLAN.md §10.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from markdown_it import MarkdownIt

DOCS_DIR = Path(settings.BASE_DIR) / "docs"


@dataclass(frozen=True)
class GuidePage:
    slug: str
    title: str
    filename: str


#: Tab order. The slug is the URL segment ("" = the guide itself).
PAGES = (
    GuidePage("", "Developer guide", "ORBIS_API_GUIDE.md"),
    GuidePage("directory", "Directory reference", "DIRECTORY_API.md"),
    GuidePage("roster", "Roster reference", "TIMELY_API.md"),
)

# Raw HTML in the Markdown is not passed through; tables are the one GFM
# extension the docs use.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


def _url(page: GuidePage) -> str:
    if page.slug:
        return reverse("developer_api_guide_page", args=[page.slug])
    return reverse("developer_api_guide")


@lru_cache(maxsize=8)
def _render(path: Path, mtime: float) -> tuple[str, list[dict]]:
    """(html, contents) for one file; cached per modification time."""
    tokens = _markdown.parse(path.read_text(encoding="utf-8"))
    contents, used = [], set()
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag not in ("h2", "h3"):
            continue
        text = tokens[index + 1].content.replace("`", "")
        anchor = slugify(text) or "section"
        while anchor in used:
            anchor += "-x"
        used.add(anchor)
        token.attrSet("id", anchor)
        if token.tag == "h2":
            contents.append({"anchor": anchor, "text": text, "children": []})
        elif contents:  # h3: listed under its section, so each endpoint is reachable
            contents[-1]["children"].append({"anchor": anchor, "text": text})
    html = _markdown.renderer.render(tokens, _markdown.options, {})
    # The docs cross-reference each other by repo path; make those clickable here.
    for page in PAGES:
        html = html.replace(
            f"<code>docs/{page.filename}</code>",
            f'<a href="{_url(page)}"><code>docs/{page.filename}</code></a>',
        )
    return html, contents


@login_required
def api_guide(request, page: str = ""):
    current = next((p for p in PAGES if p.slug == page), None)
    if current is None:
        raise Http404("No such guide page.")
    path = DOCS_DIR / current.filename
    try:
        html, contents = _render(path, path.stat().st_mtime)
    except FileNotFoundError as exc:
        raise Http404("Guide not found.") from exc
    return render(request, "accounts/api_guide.html", {
        "page_title": "Developer API",
        "current": current,
        "tabs": [{"title": p.title, "url": _url(p), "active": p == current} for p in PAGES],
        "body": mark_safe(html),  # rendered from our own repo Markdown, raw HTML disabled
        "contents": contents,
    })
