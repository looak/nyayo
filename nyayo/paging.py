"""One page size for every list on nyayo: 64, the squares of a board and the bits of a word. A list longer than that is
split over pages, `?page=2` for the page's own list and a key of its own for a list nested in another (the changed
positions of a check in a comparison), so a long gauntlet or a suite's worth of changes never makes one huge page."""

from django.core.paginator import Paginator

PAGE_SIZE = 64


def paginate(items, request, key="page"):
    """The page of `items` the request asks for (the last page for a number too high, the first for nonsense)."""
    return Paginator(items, PAGE_SIZE).get_page(request.GET.get(key))
