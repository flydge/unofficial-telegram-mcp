"""Every release version site names the package version."""
import re
import unittest
from pathlib import Path

from telegram_search_mcp import __version__, release_archive

ROOT = Path(__file__).resolve().parents[1]
DEV = re.fullmatch(r'\d+\.\d+\.\d+\.dev\d+', __version__) is not None


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding='utf-8')


class VersionConsistencyTests(unittest.TestCase):
    def test_version_grammar(self):
        # The one release version grammar (docs/releasing.md, Versions; tests/test_version_grammar.py).
        self.assertIsNotNone(release_archive._VERSION.fullmatch(__version__))

    def test_launcher_admits_and_names_the_package_version(self):
        launcher = read('packaging/launcher.py')
        self.assertIn(f"value['release_version']!='{__version__}'", launcher)
        self.assertIn(f"print('{__version__}: mcp |", launcher)

    def test_skills_pair_with_the_package_version(self):
        version = re.escape(__version__)
        self.assertRegex(read('skills/telegram-search/SKILL.md'), rf'Paired (release|candidate): `{version}`, contract 1\.')
        self.assertRegex(read('skills/telegram-setup/SKILL.md'), rf'Paired with (release|candidate) `{version}`, contract 1\.')

    def test_changelog_install_guide_and_roadmap_name_the_package_version(self):
        self.assertEqual(re.search(r'^## (\S+)', read('CHANGELOG.md'), re.M).group(1), __version__)
        self.assertIn(f'## Private bundle {__version__}', read('docs/install.md'))
        self.assertIn(f'**{__version__}**', read('docs/roadmap.md'))

    def test_bundle_fixtures_use_the_package_version(self):
        self.assertIn(f"release_version='{__version__}'", read('tests/test_bundle_archive.py'))
        self.assertIn(f"package_version='{__version__}'", read('tests/test_bundle_install.py'))

    def test_change_fixtures_build_and_expect_the_package_version(self):
        # The change harness builds its kits as the package version by default (test_account_default_goldens derives
        # its version line from that default), and these tests expect the version line a change writes
        # (docs/releasing.md, section 1; critic fix 055f).
        upgrade = read('tests/test_bundle_upgrade.py')
        self.assertIn(f"search=b'search-skill', version='{__version__}'):", upgrade)
        self.assertIn(f"b'__version__ = \"{__version__}\"\\n'", upgrade)
        for name in ('tests/test_adopt_legacy.py', 'tests/test_bundle_late_refusal.py',
                     'tests/test_capability_profiles.py'):
            with self.subTest(name=name):
                self.assertIn(f'expected_package_version = "{__version__}"', read(name))
