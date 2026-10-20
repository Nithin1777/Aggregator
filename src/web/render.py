"""
Rendering helpers for the Aggregate web UI.

The icon set is inlined as SVG so icons inherit `currentColor` and need no
extra network request.
"""
from typing import Any, Dict

from markupsafe import Markup

from src.web.icons import ICONS

_ICON_ATTRS = (
    'class="icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true" focusable="false"'
)


def icon_svg(name: str = "package", size: int = 18, cls: str = "") -> Markup:
    """Returns inline SVG markup for a Lucide icon."""
    path = ICONS.get(name) or ICONS.get("package", "")
    return Markup(
        "<svg " + _ICON_ATTRS.format(cls=cls, size=size) + ">" + path + "</svg>"
    )


def category_payload(categories: Any) -> Dict[str, Any]:
    """Shape category rows for the browser payload."""
    return {
        "slug": c["slug"],
        "label": c["label"],
        "icon": c["icon"],
        "count": c["count"],
    }
