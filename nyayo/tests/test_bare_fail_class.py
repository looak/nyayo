import re
from pathlib import Path

from django.test import SimpleTestCase

import nyayo

# `.fail` alone is the colour of a lost game, a failed bar or a key; a rule that starts at `.fail` hits every one of them
# (it once gave the score bar's red segment a margin and a border). A selector must name what it is for: `.seg.fail`, `.case-fail`.
BARE = re.compile(r'(?:^|[{},])\s*\.fail(?![\w-])', re.M)


class NoBareFailRule(SimpleTestCase):
    def test_no_template_styles_a_selector_that_starts_at_a_bare_fail_class(self):
        found = []
        for path in Path(nyayo.__file__).parent.rglob('*.html'):
            text = path.read_text(encoding='utf-8')
            for style in re.findall(r'<style[^>]*>(.*?)</style>', text, re.S):
                for hit in BARE.finditer(style):
                    found.append('%s: %s' % (path.name, style[hit.start():hit.start() + 60].strip()))
        self.assertEqual(found, [])
