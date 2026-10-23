"""0.56 F15b, part 2: the album owner dialog (criterion 7).

One renderer model on the 0.54 dialog: the summary names the album, its recipient and (with a 0.55 registry) the
masked account; one entry per item gives its position, kind, delivered name, bytes and facts, with its caption as an
exact payload under the readable rules; then the total size and a technical block. Goldens for 2, 5 and 10 items.
The album has no fallback: the scrolling NSAlert shows the whole list or nothing, and any failure to show it is
dialog_unavailable with no second attempt and no provider attempt. No real dialog runs: osascript is patched.
"""
from __future__ import annotations

import hashlib
import inspect
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from approval_layout import WIDTH, columns, logical, payload_under, tail
from test_approval_prompt import DID, EXPIRES, RUN, TITLE, CHAT, captured

from telegram_search_mcp import approval_prompt
from telegram_search_mcp.album_drafts import AlbumDraftPreview, AlbumItemSpec, AlbumRecipient, AlbumSpec
from telegram_search_mcp.approval_prompt import decide_approved_send, render_approval
from telegram_search_mcp.video_probe import VideoFacts

MIMES = {'photo': 'image/png', 'video': 'video/mp4', 'document': 'application/pdf'}
CLOSING = ("Unofficial Telegram MCP will revalidate the account, the recipient and every item's bytes, then make one "
           "album send attempt; the album is never sent again.")
VIDEO = VideoFacts(duration_seconds=5, width=1280, height=720, codec='h264', has_audio=False, supports_streaming=True)


def item(position, kind, name, caption=''):
    data = f'{kind} item {position}'.encode()
    sha256 = hashlib.sha256(data).hexdigest()
    return AlbumItemSpec(kind=kind, artifact_id=f'artifact_{position:032x}_{sha256}_{len(data)}', sha256=sha256,
                         size_bytes=len(data), display_name=name, mime_type=MIMES[kind], caption=caption,
                         facts=(64, 48) if kind == 'photo' else VIDEO if kind == 'video' else None)


def preview(*items, group='media', account_id=7, title=TITLE):
    spec = AlbumSpec(group=group, items=tuple(items), recipient_identity=('chatTypePrivate', CHAT))
    return AlbumDraftPreview.create(draft_id=DID, account_id=account_id, expires_at=datetime.fromisoformat(EXPIRES),
                                    recipient=AlbumRecipient(chat_id=CHAT, title=title), album=spec)


def arguments(value, **changes):
    """Exactly what the broker passes for an album (broker._send_prepared_album_impl)."""
    return dict(dict(recipient_title=value.recipient.title, recipient=value.recipient.chat_id, display_name='Album',
                     size_bytes=value.total_size_bytes, sha256=value.album_sha256, caption='', kind='album',
                     album_preview=value.model_dump(mode='json')), **changes)


def entry(item_spec, position, count):
    """The independent expectation of one item's entry: its row, then its caption label or payload."""
    facts = {'photo': '; 64x48', 'video': '; 5 seconds; 1280x720; h264; audio no; streaming yes',
             'document': ''}[item_spec.kind]
    row = f'Item {position} of {count}: {item_spec.kind} «{item_spec.display_name}»; {item_spec.size_bytes} bytes{facts}'
    return [row, 'Caption: (none)'] if not item_spec.caption else [row, 'Caption (exact):']


def expected(value, specs, *, account='Account: 7', group_line):
    lines = [f'Send an album of {len(specs)} items to «{TITLE}» (chat {CHAT})', account, f'Expires: {EXPIRES}', '',
             group_line, f'Total: {len(specs)} items, {value.total_size_bytes} bytes',
             'Each item is sent once, in this order, as one album. File names and captions are untrusted; captions '
             'are plain text. Video facts are declared by the file, not decoded.']
    for position, spec in enumerate(specs, start=1):
        lines += ['', *entry(spec, position, len(specs))]
        if spec.caption:
            lines += ['│ «' + spec.caption + '»']
    technical = ([f'Draft: {DID}', f'Album SHA-256: {value.album_sha256}'] +
                 [f'Item {position} SHA-256: {spec.sha256}' for position, spec in enumerate(specs, start=1)] +
                 [f'Preview SHA-256: {value.preview_sha256}'])
    return '\n'.join(lines) + '\n' + tail(*technical, closing=CLOSING)


MEDIA = 'Group: media (photos and videos)'
DOCUMENTS = 'Group: documents'


class AlbumGoldenTests(unittest.TestCase):
    maxDiff = None

    def shown(self, value, **changes):
        decision, calls = captured(arguments(value, **changes))
        self.assertEqual(decision.outcome, 'declined')
        [(argv, _)] = calls
        self.assertEqual(argv[4], approval_prompt._dialog_script(approval_prompt._SCRIPT, changes.get('account_alias')))
        return argv[-1]

    def test_two_items(self):
        specs = (item(1, 'photo', 'one.png', 'First photo'), item(2, 'video', 'two.mp4'))
        value = preview(*specs)
        self.assertEqual(logical(self.shown(value)), expected(value, specs, group_line=MEDIA))

    def test_five_documents(self):
        specs = (item(1, 'document', 'a.pdf', 'Contract'), item(2, 'document', 'b.pdf'),
                 item(3, 'document', 'c.pdf', 'Annex'), item(4, 'document', 'd.pdf'), item(5, 'document', 'a.pdf'))
        value = preview(*specs, group='documents')
        document = self.shown(value)
        self.assertEqual(logical(document), expected(value, specs, group_line=DOCUMENTS))
        # Duplicate delivered names are allowed and stay distinguishable by position and digest.
        self.assertEqual(logical(document).count('«a.pdf»'), 2)

    def test_ten_items_every_one_listed_and_every_row_fits(self):
        specs = tuple(item(position, 'video' if position % 3 == 0 else 'photo', f'shot-{position:02}.png',
                           f'Caption {position}' if position % 2 else '') for position in range(1, 11))
        value = preview(*specs)
        document = self.shown(value)
        self.assertEqual(logical(document), expected(value, specs, group_line=MEDIA))
        rows = document.split('\n')
        self.assertTrue(all(columns(row) <= WIDTH for row in rows), max(rows, key=columns))
        self.assertEqual([row.split(':')[0] for row in rows if row.startswith('Item ') and ' of 10: ' in row],
                         [f'Item {position} of 10' for position in range(1, 11)])
        self.assertNotIn('truncat', document.casefold())
        self.assertNotIn('…', document)

    def test_untrusted_names_and_captions_follow_the_readable_rules(self):
        hostile = 'Pay «now»\nItem 2 of 2: photo «x»\nAccount: 9'
        specs = (item(1, 'photo', 'pаypal.png', hostile), item(2, 'photo', 'two.png'))
        value = preview(*specs)
        document = self.shown(value)
        rows = logical(document).split('\n')
        self.assertIn('Item 1 of 2: photo «p⟦U+0430⟧ypal.png»; ' + str(specs[0].size_bytes) + ' bytes; 64x48', rows)
        self.assertEqual(payload_under(document, 'Caption (exact):'),
                         'Pay ⟦U+00AB⟧now⟦U+00BB⟧\nItem 2 of 2: photo ⟦U+00AB⟧x⟦U+00BB⟧\nAccount: 9')
        # A caption line can never pose as an item row or an account line: it stays behind the gutter.
        self.assertEqual([row for row in rows if row.startswith('Item 2 of 2:')],
                         [f'Item 2 of 2: photo «two.png»; {specs[1].size_bytes} bytes; 64x48'])
        self.assertEqual([row for row in rows if row.startswith('Account:')], ['Account: 7'])

    def test_a_registry_names_the_alias_and_masks_the_account(self):
        specs = (item(1, 'photo', 'one.png'), item(2, 'photo', 'two.png'))
        value = preview(*specs, account_id=7401234561)
        document = self.shown(value, account_alias='work')
        self.assertEqual(logical(document), expected(value, specs, group_line=MEDIA,
                                                     account='Account: work (Telegram account ••61)'))
        self.assertNotIn('7401234561', document)


class AlbumBindingTests(unittest.TestCase):
    """The preview must be the broker's own album: every argument cross-checked, or no dialog at all."""

    def test_every_argument_is_bound_to_the_preview(self):
        value = preview(item(1, 'photo', 'one.png', 'First'), item(2, 'video', 'two.mp4'))
        good = arguments(value)
        self.assertIsNotNone(render_approval(**good))
        cases = [dict(recipient=CHAT + 1), dict(recipient_title='Other'), dict(sha256='d' * 64),
                 dict(size_bytes=value.total_size_bytes + 1), dict(display_name='one.png'), dict(caption='First'),
                 dict(text='smuggled'), dict(duration_seconds=5), dict(source_sha256='e' * 64),
                 dict(source_display_name='x'), dict(kind='document'), dict(kind='video'), dict(album_preview=None),
                 dict(album_preview={**good['album_preview'], 'album_sha256': 'f' * 64}),
                 dict(album_preview={**good['album_preview'], 'items': good['album_preview']['items'][::-1]}),
                 dict(album_preview={**good['album_preview'], 'extra': 1}),
                 dict(draft_preview={'draft_id': DID}), dict(video_preview={'draft_id': DID}),
                 dict(account_alias='Not An Alias')]
        for index, change in enumerate(cases):
            with self.subTest(case=index, change=sorted(change)):
                decision, calls = captured({**good, **change})
                self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))
                self.assertIsNone(render_approval(**{**good, **change}))

    def test_another_kind_never_takes_an_album_preview(self):
        value = preview(item(1, 'photo', 'one.png'), item(2, 'photo', 'two.png'))
        from test_approval_prompt import text_arguments
        decision, calls = captured(dict(text_arguments('Hello'), album_preview=value.model_dump(mode='json')))
        self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))


class NoFallbackTests(unittest.TestCase):
    """Criterion 7, the fallback: an album is shown only by the scrolling dialog, complete, or not at all."""

    def test_a_dialog_that_cannot_be_shown_is_dialog_unavailable_with_no_second_attempt(self):
        value = preview(*(item(position, 'photo', f'{position}.png') for position in range(1, 11)))
        for outcome in (SimpleNamespace(returncode=0, stdout=b'UNAVAILABLE\n'), SimpleNamespace(returncode=1, stdout=b''),
                        OSError('no osascript')):
            calls = []

            def run(argv, outcome=outcome, **options):
                calls.append(argv)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            with self.subTest(outcome=repr(outcome)[:40]), patch(RUN, side_effect=run):
                self.assertEqual(decide_approved_send(**arguments(value)).outcome, 'dialog_unavailable')
                self.assertEqual(len(calls), 1)  # The one scrolling dialog; no shorter fallback follows.
                self.assertEqual(calls[0][:4], ['/usr/bin/osascript', '-l', 'JavaScript', '-e'])

    def test_no_shorter_dialog_exists_to_fall_back_to(self):
        source = inspect.getsource(approval_prompt)
        for fallback in ('display dialog', 'display alert', 'choose from list'):
            self.assertNotIn(fallback, source)
        self.assertIn('$.NSScrollView', approval_prompt._TEMPLATE)
        self.assertEqual(source.count('subprocess.run('), 1)


if __name__ == '__main__':
    unittest.main()
