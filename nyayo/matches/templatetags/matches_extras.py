import math

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
def thousands(value):
    """45,673: a count with its thousands marked."""
    return f"{value:,}" if isinstance(value, int) else value


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


@register.simple_tag
def evalbar_share(pawns):
    """White's share of the eval bar, in percent: lichess's winning chances, 2 / (1 + e^(-0.00368208 cp)) - 1, as a
    share; half for no score. The page's script works it out the same way when it steps through the game."""
    if pawns is None:
        return 50
    chances = 2 / (1 + math.exp(-0.00368208 * pawns * 100)) - 1
    return round(50 + 50 * chances, 1)
