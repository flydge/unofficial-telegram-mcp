"""Criterion 7: every newer reader returns provider links validated by the one shared rule."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from telegram_search_mcp import schemas
from telegram_search_mcp.exact_search_reader import ExactSearchReader
from telegram_search_mcp.history_reader import HistoryReader
from telegram_search_mcp.reply_reader import read_reply_chain
from telegram_search_mcp.tdjson import AuthorizationBlocked, TDLibError
from telegram_search_mcp.topic_history_reader import TopicHistoryReader
from test_boolean_search_reader import BooleanProvider, text
from test_exact_search_reader import SearchProvider, page
from test_history_reader import HistoryProvider
from test_message_reader import message
from test_reply_chain import reply
from test_topic_history_reader import TopicHistoryProvider, forum_message

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)
UNSAFE = ('http://t.me/c/7/{mid}', 'https://evil.example/c/7/{mid}', 'https://t.me:8443/c/7/{mid}',
          'https://user' '@t.me/c/7/{mid}','https://t.me/c/7/{mid}\u202e')


def scoped(cls, provider):
    return cls(client=provider, client_id='synthetic_client', broker_generation='synthetic_broker',
               clock=lambda: 10.0, now=lambda: NOW)


class ReaderLinkTests(unittest.TestCase):
    def readers(self):
        """Each newer reader, as (name, provider, read) over messages 40, 30 and 20 in chat 7."""
        history = HistoryProvider({i: message(i) for i in (40, 30, 20)})
        yield 'read_history', history, lambda: scoped(HistoryReader, history).read(
            schemas.ReadHistoryRequest(target=7, mode='latest', limit=3))
        topic = TopicHistoryProvider({i: forum_message(i) for i in (40, 30, 20)})
        yield 'read_topic_history', topic, lambda: scoped(TopicHistoryReader, topic).read(
            schemas.ReadTopicHistoryRequest(target=7, topic={'kind': 'forum', 'id': 9}, mode='latest', limit=3))
        chain = HistoryProvider({40: message(40, reply_to=reply(30)), 30: message(30, reply_to=reply(20)), 20: message(20)})
        yield 'read_reply_chain', chain, lambda: read_reply_chain(
            chain, schemas.ReadReplyChainRequest(anchor={'chat_id': 7, 'message_id': 40}, max_depth=3))
        exact = SearchProvider({i: message(i) for i in (40, 30, 20)})
        yield 'search_messages', exact, lambda: scoped(ExactSearchReader, exact).read(
            schemas.SearchMessagesRequest(target=7, query='selected', mode='latest', limit=3))
        rows = {i: text(i) for i in (40, 30, 20)}
        boolean = BooleanProvider(rows, {'red': [page(list(rows.values()))]})
        yield 'search_messages_boolean', boolean, lambda: scoped(ExactSearchReader, boolean).read(
            schemas.SearchMessagesRequest(target=7, mode='latest', query={'all': ['red', 'blue']}, limit=3))

    def test_every_reader_fills_validated_links_after_hydration(self):
        for name, provider, read in self.readers():
            with self.subTest(reader=name):
                provider.links = {30: UNSAFE[len(name) % len(UNSAFE)].format(mid=30), 20: TDLibError('private detail')}
                result = read()
                self.assertEqual([r.anchor.message_id for r in result.results], [40, 30, 20])
                self.assertEqual([r.message.source.telegram_url for r in result.results], ['https://t.me/c/7/40', None, None])
                # A missing or refused link is never an issue and never lowers coverage.
                self.assertTrue(all(r.status in {'complete', 'match'} and r.coverage_complete and r.issues == []
                                    for r in result.results))
                self.assertNotIn('evil.example', result.model_dump_json())
                self.assertNotIn('private detail', result.model_dump_json())
                links = [i for i, call in enumerate(provider.calls) if call[0] == 'link']
                self.assertEqual([provider.calls[i] for i in links], [('link', 7, 40), ('link', 7, 30), ('link', 7, 20)])
                # A link is requested only for a message the reader has already hydrated.
                for i in links:
                    mid = provider.calls[i][2]
                    self.assertTrue(any(call[0] in {'message', 'hydrate'} and mid in call for call in provider.calls[:i]))

    def test_lost_authorization_during_a_link_is_never_downgraded(self):
        for name, provider, read in self.readers():
            with self.subTest(reader=name):
                provider.links = {40: AuthorizationBlocked('lost')}
                result = read()
                self.assertFalse(any(r.message is not None and r.anchor.message_id == 40 for r in result.results))
                self.assertNotEqual(result.status, 'complete')


if __name__ == '__main__':
    unittest.main()
