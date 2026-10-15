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

