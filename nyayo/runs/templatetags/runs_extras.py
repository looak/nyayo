from django import template

register = template.Library()


@register.filter
def signed(value):
    """+3, -2 or 0 for a difference; empty when there is none."""
    if value is None:
        return ""
    number = f"{value:g}" if isinstance(value, float) else str(value)
    return f"+{number}" if value > 0 else number.replace("-", "−")


@register.filter
def num(value):
    """A metric without a trailing .0."""
    return f"{value:g}" if isinstance(value, float) else value


@register.filter
def sha(value, length=10):
    return (value or "")[: int(length)]


@register.filter
def trend(value):
    """A CSS class for a difference: better, worse or nothing."""
    if not value:
        return ""
    return "better" if value > 0 else "worse"


@register.filter
def outcome_label(value):
    return {"pass": "passed", "fail": "failed", "warn": "below baseline", "none": "no threshold",
            "error": "harness error"}.get(value, value)



@register.filter
def seconds(value):
    """A duration in seconds as 850 ms, 13.7 s, 4 m 05 s or 1 h 02 m; empty when there is none."""
    if value is None:
        return ""
    if value < 1:
        return f"{value * 1000:.0f} ms"
    if value < 60:
        return f"{value:.1f} s"
    minutes, rest = divmod(round(value), 60)
    if minutes < 60:
        return f"{minutes} m {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m"


@register.filter
def millis(value):
    """A duration in milliseconds, as seconds() shows it."""
    return "" if value is None else seconds(value / 1000)
