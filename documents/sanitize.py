"""HTML sanitisation for HR-editable letter templates.

``TemplateDesign.html`` and ``MessageTemplate.body_html`` are CKEditor
``RichTextField`` rows. Anyone who can reach the admin can write arbitrary
HTML there — bleach strips anything outside the allowlist before the row
is saved, so a stray ``<script>`` tag never reaches the PDF renderer.

The allowlist is intentionally narrow: tags + CSS properties WeasyPrint
actually needs for letter layout. ``data:`` URLs are allowed so embedded
inline images (logos, signatures) keep working.
"""

from __future__ import annotations

import bleach
from bleach.css_sanitizer import CSSSanitizer


ALLOWED_TAGS = [
    "p", "br", "hr", "div", "span", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "u", "s", "b", "i", "sub", "sup",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "img", "a", "blockquote", "pre", "code",
]

ALLOWED_ATTRS: dict[str, list[str]] = {
    "*": ["style", "class", "id"],
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "width", "height", "style"],
    "td": ["colspan", "rowspan", "style", "class"],
    "th": ["colspan", "rowspan", "style", "class"],
    "table": ["border", "cellspacing", "cellpadding", "style", "class"],
}

ALLOWED_PROTOCOLS = ["http", "https", "data", "mailto"]

ALLOWED_CSS_PROPERTIES = [
    "color", "background-color",
    "font-size", "font-family", "font-weight", "font-style",
    "text-align", "text-decoration", "text-transform",
    "line-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-collapse", "border-spacing", "border-radius",
    "width", "height", "max-width", "max-height", "min-width", "min-height",
    "page-break-before", "page-break-after", "page-break-inside",
    "display", "float", "clear", "vertical-align",
    "list-style", "list-style-type",
]

_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)


def clean_template_html(html: str) -> tuple[str, bool]:
    """Strip disallowed tags / attrs / CSS.

    Returns ``(cleaned, was_modified)``. ``was_modified`` lets the model save
    surface a non-blocking admin warning ("we removed X disallowed content")
    without having to diff again.
    """
    if not html:
        return html, False
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=_CSS_SANITIZER,
        strip=True,
    )
    return cleaned, cleaned != html
