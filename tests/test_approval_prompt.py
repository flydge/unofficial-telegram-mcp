"""0.54 approvals: one renderer, one native dialog, a closed fail-closed decision.

No real dialog runs here: every test patches `approval_prompt.subprocess.run`. The real JXA dialog is proven
once on the timeout path (docs/plans/0.54-conversational-usability-spec.tasks.md, section C).
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import subprocess
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

from approval_layout import LEGEND, RULE, WIDTH, columns, logical, payload_under, tail
from telegram_search_mcp import approval_prompt
from telegram_search_mcp.approval_prompt import (
    DETAILS, NOT_GIVEN, OUTCOMES, ApprovalDecision, approval_outcome, confirm_approved_send, decide_approved_send,
    render_approval)
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.forward_drafts import READABLE_RULE

DID = 'draft_' + '1' * 32
EXPIRES = '2030-01-02T03:04:05+00:00'
TITLE = 'Synthetic Recipient'
CHAT = 123
RUN = 'telegram_search_mcp.approval_prompt.subprocess.run'


def text_preview(text, **changes):
    data = text.encode()
    return dict(dict(draft_id=DID, account_id=7, recipient=CHAT, recipient_title=TITLE, kind='text',
                     expires_at=EXPIRES, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), text=text),
                **changes)


def file_preview(kind='document', data=b'synthetic file', name='report.txt', mime='text/plain', caption='', **voice):
    sha = hashlib.sha256(data).hexdigest()
    return dict(draft_id=DID, account_id=7, recipient=CHAT, recipient_title=TITLE, kind=kind, expires_at=EXPIRES,
                sha256=sha, size_bytes=len(data), artifact_id=f'artifact_{"c" * 32}_{sha}_{len(data)}',
                display_name=name, mime_type=mime, caption=caption, **voice)


def text_arguments(text, **changes):
    data = text.encode()
    return dict(dict(recipient_title=TITLE, recipient=CHAT, display_name='Text message', size_bytes=len(data),
                     sha256=hashlib.sha256(data).hexdigest(), caption='', kind='text', text=text,
                     draft_preview=text_preview(text)), **changes)


def file_arguments(preview, **changes):
    return dict(dict(recipient_title=TITLE, recipient=CHAT, display_name=preview['display_name'],
                     size_bytes=preview['size_bytes'], sha256=preview['sha256'], caption=preview['caption'],
                     kind=preview['kind'], duration_seconds=preview.get('duration_seconds'),
                     source_sha256=preview.get('source_sha256'), source_display_name=preview.get('source_display_name'),
                     draft_preview=preview), **changes)


def captured(arguments, stdout=b'DECLINED'):
    calls = []
    with patch(RUN, side_effect=lambda argv, **options: calls.append((argv, options)) or
               SimpleNamespace(returncode=0, stdout=stdout)):
        decision = decide_approved_send(**arguments)
    return decision, calls


class DecisionTests(unittest.TestCase):
    def test_only_an_approved_decision_is_truthy(self):
        self.assertEqual(OUTCOMES, {'approved', 'declined', 'timeout', 'dialog_unavailable'})
        for outcome in OUTCOMES:
            self.assertIs(bool(ApprovalDecision(outcome)), outcome == 'approved')
            # The fail-closed idiom used before 0.54 still refuses every non-approval.
            self.assertEqual(not ApprovalDecision(outcome), outcome != 'approved')
        for bad in ('APPROVED', 'not_approved', '', None, True, 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ApprovalDecision(bad)
        with self.assertRaises(FrozenInstanceError):
            ApprovalDecision('declined').outcome = 'approved'

    def test_reading_a_prompt_result_fails_closed(self):
        class Duck:
            outcome = 'approved'

            def __bool__(self):
                return True

        self.assertEqual(approval_outcome(True), 'approved')
        self.assertEqual(approval_outcome(ApprovalDecision('approved')), 'approved')
        for outcome in ('declined', 'timeout', 'dialog_unavailable'):
            self.assertEqual(approval_outcome(ApprovalDecision(outcome)), outcome)
        self.assertEqual(approval_outcome(False), 'declined')
        for value in ('approved', 'APPROVED', 'declined', 1, 1.0, None, [True], object(), Duck(), b'APPROVED'):
            with self.subTest(value=value):
                self.assertEqual(approval_outcome(value), 'dialog_unavailable')

    def test_details_are_closed_and_input_free(self):
        self.assertEqual(set(DETAILS), {'declined', 'timeout', 'dialog_unavailable'})
        self.assertEqual(len(set(DETAILS.values()) | {NOT_GIVEN}), 4)
        for detail in (*DETAILS.values(), NOT_GIVEN):
            self.assertLessEqual(len(detail), 200)
            self.assertNotIn('{', detail)

    def test_broker_uses_the_closed_decision_by_default(self):
        self.assertIs(inspect.signature(Broker.__init__).parameters['approval_prompt'].default, decide_approved_send)


class DialogTests(unittest.TestCase):
    def test_only_the_approved_token_from_a_clean_exit_approves(self):
        arguments = text_arguments('Hello')
        for stdout, outcome in ((b'APPROVED\n', 'approved'), (b'APPROVED', 'approved'), (b'DECLINED\n', 'declined'),
                                (b'TIMEOUT\n', 'timeout'), (b'DENIED\n', 'dialog_unavailable'),
                                (b'UNAVAILABLE\n', 'dialog_unavailable'), (b'', 'dialog_unavailable'),
                                (b'approved\n', 'dialog_unavailable'), (b'APPROVED\nAPPROVED', 'dialog_unavailable'),
                                (b'APPROVED TIMEOUT', 'dialog_unavailable'), (None, 'dialog_unavailable'),
                                ('APPROVED', 'dialog_unavailable')):
            with self.subTest(stdout=stdout):
                with patch(RUN, return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                    self.assertEqual(decide_approved_send(**arguments).outcome, outcome)
                with patch(RUN, return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                    self.assertIs(confirm_approved_send(**arguments), outcome == 'approved')
        for code in (1, -9, 255):
            with patch(RUN, return_value=subprocess.CompletedProcess([], code, b'APPROVED\n')):
                self.assertEqual(decide_approved_send(**arguments), ApprovalDecision('dialog_unavailable'))
        for error in (OSError('no osascript'), subprocess.TimeoutExpired(['osascript'], 125),
                      subprocess.SubprocessError('synthetic')):
            with self.subTest(error=type(error).__name__), patch(RUN, side_effect=error):
                self.assertEqual(decide_approved_send(**arguments), ApprovalDecision('dialog_unavailable'))

    def test_document_and_timer_arrive_only_as_argv(self):
        hostile = "x'); $.NSTask.launchedTaskWithLaunchPathArguments('/bin/sh', []); ('\n\"; } catch(e) {} //"
        decision, calls = captured(text_arguments(hostile))
        self.assertEqual(decision.outcome, 'declined')
        [(argv, options)] = calls
        self.assertEqual(argv[:4], ['/usr/bin/osascript', '-l', 'JavaScript', '-e'])
        self.assertEqual(argv[5:7], ['--', '120'])
        self.assertEqual(len(argv), 8)
        script, document = argv[4], argv[-1]
        self.assertEqual(script, approval_prompt._dialog_script(approval_prompt._SCRIPT))
        self.assertIn('$.NSTask', document)  # rendered, but only ever data
        self.assertNotIn('NSTask', script)
        self.assertEqual(options['timeout'], 125)
        self.assertIs(options['stdin'], subprocess.DEVNULL)
        self.assertIs(options['check'], False)
        _, calls = captured(dict(text_arguments('Hi'), timeout_seconds=5))
        self.assertEqual((calls[0][0][6], calls[0][1]['timeout']), ('5', 10))
        for seconds in (0, 121, -1, True, 5.0, '5', None):
            with self.subTest(seconds=seconds):
                decision, calls = captured(dict(text_arguments('Hi'), timeout_seconds=seconds))
                self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))

    def test_script_is_a_native_alert_with_cancel_default_and_a_timer(self):
        for template, verb, action, effect in ((approval_prompt._SCRIPT, 'Send', 'send', 'sent'),
                                               (approval_prompt._CHANGE_SCRIPT, 'Apply', 'change', 'changed')):
            script = approval_prompt._dialog_script(template)
            with self.subTest(verb=verb):
                self.assertTrue(script.startswith("ObjC.import('AppKit');\nfunction run(argv) {"))
                self.assertIn('$.NSAlert.alloc.init', script)
                self.assertIn(f'"Unofficial Telegram MCP — approve {action}"', script)
                self.assertIn(f'nothing is {effect}.', script)
                # Cancel is the first button and the default: AppKit would give a "Cancel" button Escape, so
                # Return is set explicitly; the approving button has no key equivalent at all.
                self.assertLess(script.index("addButtonWithTitle($('Cancel')).keyEquivalent = $('\\r');"),
                                script.index(f"addButtonWithTitle($('{verb}')).keyEquivalent = $('');"))
                self.assertIn('$.NSScrollView.alloc.initWithFrame($.NSMakeRect(0, 0, 740, 400))', script)
                self.assertIn('view.editable = false;', script)
                self.assertIn('view.textContainer.widthTracksTextView = false;', script)
                # On the owner's current Space, over full-screen apps, and without an appear animation:
                # without these the proof saw the alert off-Space, occluded and at alpha 0.
                self.assertIn('alert.layout;\n        alert.window.collectionBehavior = 257;\n'
                              '        alert.window.animationBehavior = 2;', script)
                self.assertIn("timerWithTimeIntervalTargetSelectorUserInfoRepeats(seconds, app, 'abortModal', $(), false)",
                              script)
                self.assertIn('addTimerForMode(timer, $.NSModalPanelRunLoopMode)', script)
                self.assertIn("argv.length !== 2 || !/^[1-9][0-9]{0,2}$/.test(argv[0]) || Number(argv[0]) > 120",
                              script)
                self.assertIn("return code === 1001 ? 'APPROVED' : code === 1000 ? 'DECLINED' : "
                              "code === -1001 ? 'TIMEOUT' : 'UNAVAILABLE';", script)
                self.assertIn("} catch (error) {\n        return 'UNAVAILABLE';", script)
                self.assertNotIn('@@', script)
        source = inspect.getsource(approval_prompt)
        self.assertNotIn('Popen', source)  # tests patch subprocess.run; a Popen switch would open real dialogs

    def test_a_change_uses_the_change_script(self):
        from test_archive_edit import archive_preview, archive_sha
        value = archive_preview()
        _, calls = captured(dict(recipient_title=value.chat.title, recipient=value.chat.chat_id,
                                 display_name='Chat list move', size_bytes=1, sha256=archive_sha(value), caption='',
                                 kind='archive', archive_preview=value.model_dump(mode='json')))
        self.assertEqual(calls[0][0][4], approval_prompt._dialog_script(approval_prompt._CHANGE_SCRIPT))


class LayoutTests(unittest.TestCase):
    maxDiff = None

    def test_reading_rule_is_the_independent_literal(self):
        self.assertEqual(READABLE_RULE, RULE)
        document, _ = render_approval(**text_arguments('Hi'))
        self.assertIn('\n\n' + LEGEND + '\n\nTechnical details:\n', logical(document))

    def test_rows_fit_the_view_and_unwrap_exactly(self):
        raw = ('Long line: ' + 'word ' * 40 + 'https://example.invalid/' + 'x' * 150 + '\n' + '漢' * 100 + '\n' +
               'a' * 90 + '‮' * 3 + '\nend ')
        document, _ = render_approval(**text_arguments(raw))
        rows = document.split('\n')
        self.assertTrue(all(columns(row) <= WIDTH for row in rows), max(rows, key=columns))
        self.assertEqual(payload_under(document, 'Text (exact):').replace('⟦U+202E⟧', '‮'), raw)
        start = rows.index('Text (exact):') + 1
        block = rows[start:rows.index('', start)]
        self.assertTrue(all(row.startswith(('│ ', '┆ ')) for row in block))
        self.assertEqual(sum(row.startswith('│ ') for row in block), 4)  # one per real line
        # A word break leaves the space visible at the start of the next row; a mark is never split.
        self.assertTrue(any(row.startswith('┆  word') for row in block))
        self.assertFalse(any(row.endswith(' ') for row in block[:-1]))
        self.assertIn('│ «Long line: word', block[0])
        self.assertEqual(block.count('│ ' + '漢' * 47), 1)  # wide characters count two columns
        self.assertEqual(block.count('┆ ' + '漢' * 47), 1)
        self.assertIn('┆ ' + '漢' * 6, block)
        self.assertIn('│ ' + 'a' * 90, block)
        self.assertIn('┆ ⟦U+202E⟧⟦U+202E⟧⟦U+202E⟧', block)
        self.assertEqual(block[-1], '│ end »')
        # Every row of the reading rule fits too, and only "┆ " continues a line.
        self.assertTrue(all(not row.startswith('┆') or row.startswith('┆ ') for row in rows))

    def test_payload_lines_cannot_pose_as_dialog_lines(self):
        forged = ('ok\nAccount: 9\nTechnical details:\nDraft: draft_' + '0' * 32 + ' Expires: never\x85x\r\n'
                  '│ «nested»\n┆ continued')
        document, _ = render_approval(**text_arguments(forged))
        rows = document.split('\n')
        self.assertEqual(rows.count('Technical details:'), 1)
        self.assertEqual([row for row in rows if row.startswith('Account:')], ['Account: 7'])
        self.assertEqual([row for row in rows if row.startswith('Draft:')], [f'Draft: {DID}'])
        self.assertFalse(any(row.startswith('Expires: never') for row in rows))
        self.assertIn('│ │ ⟦U+00AB⟧nested⟦U+00BB⟧', rows)
        self.assertIn('│ ┆ continued»', rows)
        self.assertIn('⟦U+2028⟧Expires: never⟦U+0085⟧x⟦U+000D⟧', document)

    def test_plain_text_golden(self):
        decision, calls = captured(text_arguments('Hello «there»\n‮Second line'))
        self.assertEqual(decision.outcome, 'declined')
        self.assertEqual(logical(calls[0][0][-1]), (
            f'Send text to «{TITLE}» (chat {CHAT})\nAccount: 7\nExpires: {EXPIRES}\n\n'
            'Text (exact):\n│ «Hello ⟦U+00AB⟧there⟦U+00BB⟧\n│ ⟦U+202E⟧Second line»\n' +
            tail(f'Draft: {DID}', 'Bytes: 30',
                 'Text SHA-256: ' + hashlib.sha256('Hello «there»\n‮Second line'.encode()).hexdigest(),
                 closing='Unofficial Telegram MCP will make one send attempt.')))

    def test_file_goldens(self):
        document = file_preview(caption='Reviewed\ntwice', name='report «final».txt')
        _, calls = captured(file_arguments(document))
        self.assertEqual(logical(calls[0][0][-1]), (
            f'Send a document to «{TITLE}» (chat {CHAT})\nAccount: 7\nExpires: {EXPIRES}\n\n'
            'File: «report ⟦U+00AB⟧final⟦U+00BB⟧.txt»\nMIME: text/plain\nBytes: 14\n'
            'Caption (exact):\n│ «Reviewed\n│ twice»\n' +
            tail(f'Draft: {DID}', f"SHA-256: {document['sha256']}",
                 closing='Unofficial Telegram MCP will make one upload/send attempt.')))
        photo = file_preview('photo', b'\x89PNG synthetic', 'shot.png', 'image/png')
        _, calls = captured(file_arguments(photo))
        self.assertIn('\nFile: «shot.png»\nMIME: image/png\nBytes: 14\nCaption: (none)\n', logical(calls[0][0][-1]))
        self.assertTrue(calls[0][0][-1].startswith(f'Send a photo to «{TITLE}» (chat {CHAT})\n'))
        voice = file_preview('voice_note', b'OggS synthetic', 'memo.ogg', 'audio/ogg', duration_seconds=3,
                             waveform_base64='AAAA', source_sha256='b' * 64, source_display_name='memo.m4a',
                             converted=True)
        _, calls = captured(file_arguments(voice))
        self.assertEqual(logical(calls[0][0][-1]), (
            f'Send a voice note to «{TITLE}» (chat {CHAT})\nAccount: 7\nExpires: {EXPIRES}\n\n'
            'File: «memo.ogg»\nMIME: audio/ogg\nBytes: 14\nSource file: «memo.m4a»\nDuration: 3 seconds\n'
            'Converted to OGG/Opus mono: True\nCaption: (none)\n' +
            tail(f'Draft: {DID}', f"SHA-256: {voice['sha256']}", 'Source SHA-256: ' + 'b' * 64,
                 closing='Unofficial Telegram MCP will make one upload/send attempt.')))

    def test_formatted_text_golden(self):
        from test_formatting_public_owner import CHAT as FCHAT, TEXT, preview
        value = preview()
        data = TEXT.encode()
        _, calls = captured(dict(recipient_title='Chat', recipient=FCHAT, display_name='Text message',
                                 size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), caption='',
                                 kind='text', text=TEXT, topic_preview=value))
        self.assertEqual(logical(calls[0][0][-1]), (
            f'Send formatted text to «Chat» (chat {FCHAT})\nAccount: 7\nExpires: {EXPIRES}\n\n'
            'Text (exact):\n│ «A😀B cafe»\n'
            'Outgoing styles (final normalized text; UTF-16 code units):\n'
            'textEntityTypeBold: offset 0, length 5; covered «A😀B »\n'
            'textEntityTypeSpoiler: offset 1, length 2; covered «😀»\n\n'
            'Normal notifications; no reply, link preview, schedule or draft clearing.\n' +
            tail(f'Draft: {DID}', 'Bytes: 11', f'Text SHA-256: {hashlib.sha256(data).hexdigest()}',
                 f"Preview SHA-256: {value['preview_sha256']}",
                 closing='Unofficial Telegram MCP will revalidate this account and recipient, including any selected '
                         'topic, then make one send attempt.')))

    def reply_artifact(self, draft, text='Source line, second «line»'):
        from telegram_search_mcp.reply_artifact_drafts import preview_facts
        from telegram_search_mcp.reply_drafts import ReplyTarget, digest
        from telegram_search_mcp.draft_models import DraftPreview
        target = ReplyTarget(anchor={'chat_id': CHAT, 'message_id': 55}, text='[untrusted Telegram evidence] ' + text,
                             source_sha256='a' * 64, sanitized=True)
        value = DraftPreview.model_validate(draft)
        return {'draft': value.model_dump(mode='json'), 'reply_target': target.model_dump(mode='json'),
                'preview_sha256': digest(preview_facts(value, target))}

    def test_reply_artifact_goldens(self):
        document = file_preview(caption='See attached')
        value = self.reply_artifact(document)
        arguments = file_arguments(document, draft_preview=None, reply_artifact_preview=value)
        _, calls = captured(arguments)
        self.assertEqual(logical(calls[0][0][-1]), (
            f'Send a document reply to «{TITLE}» (chat {CHAT})\nAccount: 7\nExpires: {EXPIRES}\n\n'
            f'In reply to message 55 in chat {CHAT} [untrusted Telegram evidence]:\n'
            '│ «Source line, second ⟦U+00AB⟧line⟦U+00BB⟧»\n\n'
            'File: «report.txt»\nMIME: text/plain\nBytes: 14\nCaption (exact):\n│ «See attached»\n' +
            tail(f'Draft: {DID}', f"SHA-256: {document['sha256']}", 'Target sanitized: True; truncated: False',
                 'Target source SHA-256: ' + 'a' * 64, f"Preview SHA-256: {value['preview_sha256']}",
                 closing='Unofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.')))
        waveform = base64.b64encode(bytes(range(63))).decode()
        voice = file_preview('voice_note', b'OggS synthetic', 'memo.ogg', 'audio/ogg', duration_seconds=3,
                             waveform_base64=waveform, source_sha256='b' * 64, source_display_name='memo.m4a',
                             converted=True)
        value = self.reply_artifact(voice, 'Voice please')
        _, calls = captured(file_arguments(voice, draft_preview=None, reply_artifact_preview=value))
        shown = logical(calls[0][0][-1])
        self.assertTrue(shown.startswith(f'Send a voice note reply to «{TITLE}» (chat {CHAT})\n'))
        self.assertIn('\nFile: «memo.ogg»\nMIME: audio/ogg\nBytes: 14\nSource file: «memo.m4a»\n'
                      'Duration: 3 seconds\nConverted to OGG/Opus mono: True\nCaption: (none)\n', shown)
        self.assertIn(f'\nSource SHA-256: {"b" * 64}\nWaveform (base64): {waveform}\n', shown)
        # The reply-artifact preview is now bound to the broker's own arguments like every other kind.
        for key, changed in (('recipient', CHAT + 1), ('recipient_title', 'Other'), ('sha256', 'd' * 64),
                             ('size_bytes', 1), ('display_name', 'other.ogg'), ('caption', 'x'), ('kind', 'document'),
                             ('text', 'smuggled')):
            with self.subTest(key=key):
                decision, calls = captured(dict(file_arguments(voice, draft_preview=None, reply_artifact_preview=value),
                                                **{key: changed}))
                self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))

    def test_unbound_or_competing_previews_show_no_dialog(self):
        text = text_arguments('Hello')
        document = file_preview()
        cases = [dict(text, draft_preview=None), dict(text, recipient=CHAT + 1), dict(text, recipient_title='Other'),
                 dict(text, text='Other'), dict(text, sha256='d' * 64), dict(text, caption='x'),
                 dict(text, display_name='report.txt'), dict(text, kind='document'),
                 dict(text, duration_seconds=3), dict(text, draft_preview=text_preview('Hello', kind='document')),
                 dict(text, draft_preview=text_preview('Hello', account_id=0)),
                 dict(text, draft_preview=dict(text_preview('Hello'), extra=1)),
                 dict(file_arguments(document), caption='other'), dict(file_arguments(document), display_name='x.txt'),
                 dict(file_arguments(document), kind='photo'), dict(file_arguments(document), text='smuggled'),
                 dict(file_arguments(document), duration_seconds=3), dict(file_arguments(document), kind='forward')]
        from test_formatting_public_owner import preview
        cases.append(dict(text, topic_preview=preview()))
        for index, arguments in enumerate(cases):
            with self.subTest(case=index):
                decision, calls = captured(arguments)
                self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))
                self.assertIsNone(render_approval(**arguments))


if __name__ == "__main__":
    unittest.main()
