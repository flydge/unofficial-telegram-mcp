"""0.56 F15b, part 1 (core): ordered albums with per-item outcomes and no resend.

Synthetic artifacts and a synthetic raw TDLib transport only: the contracts, drafts, staging, transport
validator, observation reducer and broker are production code. No live TDLib and no owner dialog.
"""
from __future__ import annotations

import unittest

from telegram_search_mcp.config import (CAPABILITY_PROFILES, KNOWN_CAPABILITIES, LEGACY_CAPABILITIES,
                                        PROFILE_DEPENDENCIES, profile_of, unlocking_profiles)
from telegram_search_mcp.contract import CapabilityName, SEND_DEPENDENT_CAPABILITIES, required_capabilities


class CapabilityTests(unittest.TestCase):
    """Work item 1: album_send is default off and requires send; video items also need video_send."""

    def test_album_send_is_a_known_default_off_send_dependent_capability(self):
        self.assertIn('album_send', KNOWN_CAPABILITIES)
        self.assertNotIn('album_send', LEGACY_CAPABILITIES)
        self.assertIn('album_send', SEND_DEPENDENT_CAPABILITIES)
        self.assertEqual(required_capabilities('album_send'), ('album_send', 'send'))
        self.assertIn('album_send', CapabilityName.__args__)

    def test_album_send_has_its_own_profile_that_depends_on_send_only(self):
        # Lead decision: never inside `video`, so enabling albums grants no video_send; video items in an album
        # need the `video` profile as well (the broker's per-item conjunction).
        self.assertEqual(profile_of('album_send'), 'albums')
        self.assertEqual(CAPABILITY_PROFILES['albums'], ('album_send',))
        self.assertEqual(PROFILE_DEPENDENCIES['albums'], ('send',))
        self.assertEqual(unlocking_profiles(['album_send']), ('albums',))
        self.assertEqual(unlocking_profiles(['album_send', 'send']), ('albums',))
        self.assertEqual(unlocking_profiles(['album_send', 'video_send']), ('video', 'albums'))

    def test_profile_switches_keep_the_dependency_rules(self):
        from telegram_search_mcp import capability_profiles as profiles
        enabled = profiles.planned(('artifacts', 'read'), 'enable', 'albums')
        self.assertIn('album_send', enabled)
        self.assertTrue(set(CAPABILITY_PROFILES['send']) <= set(enabled))
        self.assertNotIn('video_send', enabled)
        everything = tuple(sorted({'artifacts', 'read', *(c for m in CAPABILITY_PROFILES.values() for c in m)}))
        self.assertNotIn('album_send', profiles.planned(everything, 'disable', 'send'))
        self.assertIn('album_send', profiles.planned(everything, 'disable', 'video'))
        self.assertNotIn('video_send', profiles.planned(everything, 'disable', 'video'))
        self.assertEqual(profiles.states(('artifacts', 'read', 'send', 'album_send'))['albums'], 'on')


if __name__ == '__main__':
    unittest.main()
