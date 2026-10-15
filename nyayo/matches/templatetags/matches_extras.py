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


@register.filter
def clock(milliseconds):
    """A clock as m:ss, with tenths under twenty seconds, h:mm:ss over an hour; a dash when unknown."""
    if milliseconds is None:
        return "–"
    seconds = max(0.0, milliseconds / 1000)
    if seconds < 20:
        return f"0:{seconds:04.1f}"
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@register.filter
def human(value):
    """12.3k, 4.5M: a big count short enough for a small box; empty for none."""
    if value is None:
        return ""
    from nyayo.matches.pgnview import human as short
    return short(value)


@register.filter
def spent(seconds):
    """The time a move took, 0.9 s or 12.4 s."""
    return "" if seconds is None else f"{seconds:.1f} s"
