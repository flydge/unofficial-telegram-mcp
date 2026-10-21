"""Independent expectations for the 0.54 owner dialog layout.

The dialog document is a summary, the exact payload behind a fixed gutter, the reading rule and a technical
block. Visual rows are at most 96 columns; a longer line continues on rows that start with "┆ ", and a row
breaks before a space, so removing every "\\n┆ " restores the logical lines exactly. Goldens compare the
logical form; `test_approval_prompt` pins the rows themselves.
"""
from __future__ import annotations

import unicodedata

# Independent literal of the dialog marking rule; a silent change fails every golden.
RULE = ('⟦U+XXXX⟧ marks control, format, private-use, unassigned and default-ignorable characters '
        '(except one U+FE0E/U+FE0F right after a non-selector character), line, paragraph and non-ASCII '
        'spaces, every repeated space, U+2800, characters NFKC changes, combining marks after the second '
        'in a row, « » ⟦ 〚 〛, and Cyrillic, Greek, Armenian, Cherokee, Lisu or undecomposed non-ASCII '
        'Latin letters in a word that also has ASCII letters; look-alike words in one script are not marked')
LEGEND = ('Reading rule: text in « » is exact; ' + RULE + '. A row starting with │ is one payload line; '
          'a row starting with ┆ continues the row above, and a space right after ┆ belongs to the text.')
MARK = '[untrusted Telegram evidence]'
WIDTH = 96


def logical(document: str) -> str:
    return document.replace('\n┆ ', '')


def columns(row: str) -> int:
    return sum(0 if unicodedata.category(char) in ('Mn', 'Me') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in row)


def payload_under(document: str, label: str) -> str:
    """The rendered payload right below the line `label`: its gutter rows joined by real line breaks,
    without the enclosing « »."""
    lines = logical(document).split('\n')
    rows = []
    for line in lines[lines.index(label) + 1:]:
        if not line.startswith('│ '):
            break
        rows.append(line[2:])
    body = '\n'.join(rows)
    assert body.startswith('«') and body.endswith('»'), body[:80]
    return body[1:-1]


def tail(*technical: str, closing: str) -> str:
    """Reading rule, technical block and closing statement, as logical lines."""
    return '\n'.join(['', LEGEND, '', 'Technical details:', *technical, '', closing])
