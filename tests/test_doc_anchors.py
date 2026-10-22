"""Every in-repository Markdown link to a heading anchor names a heading that exists (GitHub's anchor rules).

0.53.0rc1 re-review: renaming `## Private bundle 0.53.0.dev0` left two links to its old anchor. A version in a heading
moves its anchor at every bump, so the links are checked rather than remembered.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'\]\(([^)\s#]*\.md)?#([^)\s]+)\)')


def slug(heading):
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading.strip().lower())
    text = re.sub(r'[`*]', '', text)  # GitHub keeps underscores in anchors (`next_action` -> next_action)
    return re.sub(r'[^\w\- ]', '', text).replace(' ', '-')


def anchors(path):
    found, seen, fence = set(), {}, False
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('```'):
            fence = not fence
            continue
        match = None if fence else re.match(r'(#{1,6}) (.*)', line)
        if match:
            name = slug(match.group(2))
            count = seen.get(name, 0)
            found.add(name if count == 0 else '%s-%d' % (name, count))
            seen[name] = count + 1
    return found


def documents():
    return [ROOT / 'README.md', ROOT / 'CHANGELOG.md', *sorted((ROOT / 'docs').rglob('*.md')),
            *sorted((ROOT / 'skills').rglob('*.md')), *sorted((ROOT / 'plugins').rglob('*.md'))]


class DocumentAnchorTests(unittest.TestCase):
    def test_the_slug_follows_github(self):
        self.assertEqual(slug('Private bundle 0.53.0rc2'), 'private-bundle-0530rc2')
        self.assertEqual(slug('A refusal after the broker stop'), 'a-refusal-after-the-broker-stop')
        self.assertEqual(slug('0.53.0 — 2026-10-22'), '0530--2026-10-22')
        self.assertEqual(slug('`uninstall` and [rollback](x.md)'), 'uninstall-and-rollback')

    def test_every_heading_link_resolves(self):
        broken = []
        for document in documents():
            for target, anchor in LINK.findall(document.read_text(encoding='utf-8')):
                destination = (document.parent / target).resolve() if target else document
                if destination.is_file() and anchor not in anchors(destination):
                    broken.append('%s -> %s#%s' % (document.relative_to(ROOT), target, anchor))
        self.assertEqual(broken, [])


if __name__ == '__main__':
    unittest.main()
