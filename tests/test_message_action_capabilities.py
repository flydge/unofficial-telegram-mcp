"""0.57 work item 2: the four own-message action capabilities and their profiles.

edit_own_caption, edit_own_styled_text, pin_messages and delete_own_message are default off, and each requires
`send` and `read` (its prepare step reveals message content). Lead decision (owner review pending): the two edit
capabilities join `archive_edit`, the profile of every own-message edit, so a styled caption or a styled text edit
is unlocked by one profile; pinning and deleting each get a profile of their own (`pin`, `delete`), which depend on
`send` only, so enabling edits never grants a broadcast pin or an irreversible delete.
"""
from __future__ import annotations

import unittest

from telegram_search_mcp import capability_profiles as profiles
from telegram_search_mcp.config import (CAPABILITY_PROFILES, KNOWN_CAPABILITIES, LEGACY_CAPABILITIES,
                                        PROFILE_DEPENDENCIES, RuntimePolicy, profile_of, unlocking_profiles)
from telegram_search_mcp.contract import (CapabilityName, READ_DEPENDENT_CAPABILITIES, SEND_DEPENDENT_CAPABILITIES,
                                          required_capabilities)

NEW = ('edit_own_caption', 'edit_own_styled_text', 'pin_messages', 'delete_own_message')


class CapabilityTests(unittest.TestCase):
    def test_each_is_known_default_off_and_requires_send_and_read(self):
        for name in NEW:
            with self.subTest(name=name):
                self.assertIn(name, KNOWN_CAPABILITIES)
                self.assertIn(name, CapabilityName.__args__)
                self.assertNotIn(name, LEGACY_CAPABILITIES)
                self.assertNotIn(name, RuntimePolicy().enabled_capabilities)
                self.assertIn(name, SEND_DEPENDENT_CAPABILITIES)
                self.assertIn(name, READ_DEPENDENT_CAPABILITIES)
                self.assertEqual(required_capabilities(name), (name, 'send', 'read'))

    def test_formatted_text_send_stays_a_sending_capability(self):
        # A styled edit needs edit_own_styled_text, never formatted_text_send (spec 3, Capabilities).
        self.assertEqual(required_capabilities('formatted_text_send'), ('formatted_text_send',))
        for name in NEW:
            self.assertNotIn('formatted_text_send', required_capabilities(name))


class ProfileTests(unittest.TestCase):
    def test_the_edits_join_archive_edit_and_pin_and_delete_have_their_own_profiles(self):
        self.assertEqual(CAPABILITY_PROFILES['archive_edit'],
                         ('archive_chats', 'edit_own_text', 'edit_own_caption', 'edit_own_styled_text'))
        self.assertEqual(CAPABILITY_PROFILES['pin'], ('pin_messages',))
        self.assertEqual(CAPABILITY_PROFILES['delete'], ('delete_own_message',))
        self.assertEqual(list(CAPABILITY_PROFILES)[-2:], ['pin', 'delete'])
        for name in ('archive_edit', 'pin', 'delete'):
            self.assertEqual(PROFILE_DEPENDENCIES[name], ('send',))
        self.assertEqual([profile_of(name) for name in NEW], ['archive_edit', 'archive_edit', 'pin', 'delete'])
        # Every optional capability still belongs to exactly one profile.
        members = [member for names in CAPABILITY_PROFILES.values() for member in names]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), KNOWN_CAPABILITIES - {'read', 'artifacts'})

    def test_unlocking_answers_name_the_one_profile(self):
        self.assertEqual(unlocking_profiles(['edit_own_caption', 'edit_own_styled_text']), ('archive_edit',))
        self.assertEqual(unlocking_profiles(['edit_own_styled_text', 'send']), ('archive_edit',))
        self.assertEqual(unlocking_profiles(['pin_messages']), ('pin',))
        self.assertEqual(unlocking_profiles(['delete_own_message', 'send']), ('delete',))
        self.assertEqual(unlocking_profiles(['pin_messages', 'delete_own_message']), ('pin', 'delete'))
        self.assertIsNone(unlocking_profiles(['edit_own_caption', 'read']))  # read is base, never a profile

    def test_enabling_edits_never_grants_pin_or_delete_and_disabling_send_removes_all(self):
        enabled = set(profiles.planned(('artifacts', 'read'), 'enable', 'archive_edit'))
        self.assertTrue({'edit_own_caption', 'edit_own_styled_text', 'send'} <= enabled)
        self.assertFalse({'pin_messages', 'delete_own_message'} & enabled)
        self.assertEqual(set(profiles.planned(('artifacts', 'read'), 'enable', 'delete')) - set(CAPABILITY_PROFILES['send']),
                         {'artifacts', 'read', 'delete_own_message'})
        everything = tuple(sorted({'artifacts', 'read', *(c for m in CAPABILITY_PROFILES.values() for c in m)}))
        after = set(profiles.planned(everything, 'disable', 'send'))
        self.assertFalse(after & set(NEW))
        self.assertEqual(set(everything) - set(profiles.planned(everything, 'disable', 'delete')),
                         {'delete_own_message'})
        self.assertEqual(profiles.states(('artifacts', 'read', 'send', 'archive_chats', 'edit_own_text'))['archive_edit'],
                         'partial')


if __name__ == '__main__':
    unittest.main()
