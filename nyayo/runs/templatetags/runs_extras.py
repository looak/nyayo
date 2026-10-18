from django import template

from nyayo.paging import paginate

register = template.Library()


@register.simple_tag(takes_context=True)
def paged(context, items, key="page"):
    """A list as the page of it the request asks for: `{% paged things "page" as page %}`. Nested lists on one page
    each need a key of their own."""
    return paginate(items, context["request"], key)


@register.inclusion_tag("runs/_pager.html", takes_context=True)
def pager(context, page, key="page"):
    """Previous and next links for a page from `paged` (or a view), keeping the rest of the query as it is. Nothing
    for a list that fits on one page."""
    request = context["request"]

    def link(number):
        query = request.GET.copy()
        query[key] = number
        return "?" + query.urlencode()

    return {
        "page": page,
        "first": link(1) if page.number > 2 else None,
        "previous": link(page.previous_page_number()) if page.has_previous() else None,
        "next": link(page.next_page_number()) if page.has_next() else None,
        "last": link(page.paginator.num_pages) if page.number < page.paginator.num_pages - 1 else None,
    }


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
