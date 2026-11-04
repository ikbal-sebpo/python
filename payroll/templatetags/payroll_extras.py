from django import template

register = template.Library()


@register.filter
def hhmmss(total_seconds):
    """Render an integer seconds count as HH:MM:SS for upload previews/reports."""
    try:
        total_seconds = int(total_seconds)
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
