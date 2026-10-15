from django import template

register = template.Library()


@register.filter
def elo(value):
    """+12.3 or −45.0; a dash where there's no figure (no games, or a clean sweep)."""
    if value is None:
        return "–"
    text = f"{value:+.1f}"
    return text.replace("-", "−")


@register.filter
def error(value):
    return "" if value is None else f"± {value:.1f}"


@register.filter
def percent(value):
    return "–" if value is None else f"{value * 100:.1f}%"


@register.filter
def pawns(value):
    return "" if value is None else f"{value:+.2f}"


@register.filter
def pair(value, key):
    """A { subject, opponent } count from a result row, 0 when missing."""
    return (value or {}).get(key, 0)


@register.filter
def dictget(value, key):
    return (value or {}).get(key)
