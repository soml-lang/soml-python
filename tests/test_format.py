"""
Tests for `soml.format()`, ported from `test/format.ts` in the JS reference implementation, soml-js, which gives the same text for every document.
"""

import random
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from helpers import best_time

import soml
from soml import tree

ROOT = Path(__file__).parent / 'conformance' / 'valid'


def readable_documents() -> list[tuple[str, str]]:
	"""Every valid case whose values Python can hold, with its canonical form."""
	documents: list[tuple[str, str]] = []

	for path in sorted(ROOT.rglob('*.soml')):
		if path.name.endswith(('.canonical.soml', '.formatted.soml')):
			continue

		text = path.read_bytes().decode()

		try:
			soml.loads(text)
		except soml.SOMLDecodeError:
			continue

		documents.append((text, path.with_name(path.name.removesuffix('.soml') + '.canonical.soml').read_bytes().decode()))

	return documents


DOCUMENTS = readable_documents()


def canonical(text: str) -> str:
	"""Canonical form is unique for a value, so it compares values, types included."""
	return soml.dumps(soml.loads(text), canonical=True)


def comment_texts(text: str) -> list[str]:
	"""The comments, without the changes the formatter makes inside them: trailing whitespace, and runs of blank lines in a block comment."""
	return [re.sub(r'\n{3,}', '\n\n', re.sub(r'[\t ]+$', '', comment.value, flags=re.MULTILINE)) for comment in tree.parse(text).comments]


@dataclass
class Layout:
	"""
	A document as its tokens and comments, and the whitespace around them: `gaps[0]` comes before the first item, and `gaps[index + 1]` after `items[index]`. Nothing may be written in a fixed gap, which is inside a key.
	"""

	items: list[tuple[str, str]]
	gaps: list[str]
	fixed_gaps: set[int]

	def join(self, items: list[tuple[str, str]] | None = None, gaps: list[str] | None = None) -> str:
		items = self.items if items is None else items
		gaps = self.gaps if gaps is None else gaps
		return gaps[0] + ''.join(text + gaps[index + 1] for index, (_, text) in enumerate(items))


def split_layout(text: str) -> Layout:
	document = tree.parse(text)
	keys = [node.range for node in tree.walk(document) if isinstance(node, tree.KeyNode) and node.range is not None]
	pieces = sorted([(token.kind, token.range) for token in document.tokens] + [(comment.kind, comment.range) for comment in document.comments], key=lambda piece: piece[1][0])
	gaps: list[str] = []
	fixed_gaps: set[int] = set()

	for index, (_, (start, _)) in enumerate(pieces):
		gap_start = pieces[index - 1][1][1] if index > 0 else 0
		gaps.append(text[gap_start:start])

		if any(key_start < gap_start <= key_end for key_start, key_end in keys):
			fixed_gaps.add(index)

	gaps.append(text[pieces[-1][1][1] :])
	return Layout([(kind, text[start:end]) for kind, (start, end) in pieces], gaps, fixed_gaps)


def change_spacing(layout: Layout, generator: random.Random) -> str:
	"""The same document with other spaces and tabs between its tokens and comments. Line breaks are kept."""
	return layout.join(gaps=[gap if index in layout.fixed_gaps else '\n'.join(generator.choice(['', '', ' ', '\t', ' \t ']) for _ in gap.split('\n')) for index, gap in enumerate(layout.gaps)])


def change_layout(layout: Layout, generator: random.Random) -> str:
	"""The same value with comments and line breaks added between tokens, and the trailing comma toggled in some brackets."""
	items = list(layout.items)
	gaps: list[str] = []

	for index, gap in enumerate(layout.gaps):
		if index in layout.fixed_gaps or generator.random() < 0.6:
			gaps.append(gap)
			continue

		# A comma must be on the line of the item before it, so no line break outside a comment goes before one, even with comments between them.
		is_before_comma = next((text for kind, text in items[index:] if kind != 'block'), None) == ','
		same_line = [' ', f'/* added {index} */', f'/* added {index}\n */']
		addition = generator.choice(same_line if is_before_comma else [*same_line, '\n', '\n\n', f'# added {index}\n'])
		# Text directly after a line comment would be part of it, so the addition goes on the next line.
		gaps.append(f'{gap}\n{addition}' if index > 0 and items[index - 1][0] == 'line' else f'{addition}{gap}')

	for index, (kind, text) in enumerate(items):
		if kind != 'punctuator' or text not in {']', '}'} or generator.random() < 0.5:
			continue

		last = max(candidate for candidate in range(index) if items[candidate][0] not in {'line', 'block'})
		last_kind, last_text = items[last]

		if last_text == ',':
			items[last] = (last_kind, '')
		elif last_text not in {'[', '{'}:
			items[last] = (last_kind, f'{last_text},')

	return layout.join(items, gaps)


@pytest.mark.parametrize(('text', 'canonical_text'), DOCUMENTS)
def test_conformance(text: str, canonical_text: str) -> None:
	formatted = soml.format(text)
	assert canonical(formatted) == canonical(text), 'The value changed.'
	assert soml.format(formatted) == formatted, 'Formatting again changed the text.'
	assert comment_texts(formatted) == comment_texts(text), 'A comment changed.'
	# Canonical form is already formatted.
	assert soml.format(canonical_text) == canonical_text

	layout = split_layout(text)
	generator = random.Random(text)

	for _ in range(3):
		respaced = change_spacing(layout, generator)
		assert soml.format(respaced) == formatted, f'Spaces and tabs between tokens changed the output, for {respaced!r}'

		changed = change_layout(layout, generator)
		changed_formatted = soml.format(changed)
		assert canonical(changed_formatted) == canonical(text), f'The value changed, for {changed!r}'
		assert soml.format(changed_formatted) == changed_formatted, f'Formatting again changed the text, for {changed!r}'
		assert comment_texts(changed_formatted) == comment_texts(changed), f'A comment changed, for {changed!r}'


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		pytest.param(
			'a: {b: 1, c: [1,\n2], d: {}, e: [\n]}',
			'a: {\n→b: 1\n→c: [\n→→1\n→→2\n→]\n→d: {}\n→e: []\n}\n',
			id='one tab per level and every member and item of a container that spans lines on its own line',
		),
		pytest.param('a: {b: 1,  c: [1,2,], d: {}}', 'a: {b: 1, c: [1, 2], d: {}}\n', id='a container on one line stays on one line'),
		pytest.param('m: [\n[1,0], [0, 1]\n]', 'm: [\n→[1, 0]\n→[0, 1]\n]\n', id='the rows of a matrix stay on their lines'),
		pytest.param('a: [[ /* x */ ], 1]', 'a: [[/* x */], 1]\n', id='a container on one line that holds only a comment'),
		pytest.param('{a: 1}', '{a: 1}\n', id='a braced document on one line stays on one line'),
		pytest.param('{\na: 1}', '{\n→a: 1\n}\n', id='a braced document stays braced'),
		pytest.param('[1,\n[2]]', '[\n→1\n→[2]\n]\n', id='a top-level array is indented'),
		pytest.param('a:1\nb:    2', 'a: 1\nb: 2\n', id='one space after the colon'),
		pytest.param('a:\n\t1', 'a: 1\n', id='a value on the line after its key moves up'),
		pytest.param("'b c': [0xFF, 1_000, 90m, \"x\", 'y', 2026-09-19T16:00:00+02:00]", "'b c': [0xFF, 1_000, 90m, \"x\", 'y', 2026-09-19T16:00:00+02:00]\n", id='spelling is kept'),
		pytest.param('\n\n# Header   \n\n\n\na: 1  \n\n\n\nb: 2\n\n\n', '# Header\n\na: 1\n\nb: 2\n', id='trailing whitespace is removed and runs of blank lines become one'),
		pytest.param('a: [\n\n1,\n\n2,\n\n]', 'a: [\n→1\n\n→2\n]\n', id='no blank line directly inside brackets'),
		pytest.param('a: {\n\nb: 1\n\n}', 'a: {\n→b: 1\n}\n', id='no blank line directly inside braces'),
		pytest.param('a: [1, # One\n2 # Two\n]', 'a: [\n→1 # One\n→2 # Two\n]\n', id='a comment at the end of a line stays there when its comma is removed'),
		pytest.param('a: [1 /* One */\n]', 'a: [\n→1 /* One */\n]\n', id='a block comment at the end of a line stays there'),
		pytest.param('[1, /* x */ 2\n]', '[\n→1\n→/* x */ 2\n]\n', id='a block comment before a value on the same line stays in front of it'),
		pytest.param('a: [1 /* a */, /* b */ 2\n]', 'a: [\n→1 /* a */\n→/* b */ 2\n]\n', id='block comments on both sides of a comma'),
		pytest.param('[1, /* a */ /* b */ 2\n]', '[\n→1\n→/* a */ /* b */ 2\n]\n', id='two block comments before a value'),
		pytest.param(
			'[1 /* a */, /* b */ 2, /* c */]', '[1 /* a */, /* b */ 2 /* c */]\n', id='block comments stay among the items and commas of a container on one line, and the trailing comma goes'
		),
		pytest.param('/* a */ /* b */ a: 1', '/* a */ /* b */ a: 1\n', id='two block comments before a member'),
		pytest.param('a: [\n1,\n# Note\n2,\n# End\n]', 'a: [\n→1\n→# Note\n→2\n→# End\n]\n', id='a comment on its own line stays on its own line'),
		pytest.param('a: [ # c\n1]', 'a: [ # c\n→1\n]\n', id='a comment after the opening bracket stays on its line'),
		pytest.param('a: { # only\n}', 'a: { # only\n}\n', id='a comment in an empty object stays after the opening brace'),
		pytest.param('{#\n\n1: []}', '{ #\n→1: []\n}\n', id='a comment after the opening brace of the document'),
		pytest.param('a: [\n# only\n]', 'a: [\n→# only\n]\n', id='an empty collection with comments is laid out like a non-empty one'),
		pytest.param('a: /* c */ 1', 'a: /* c */ 1\n', id='a block comment after the colon'),
		pytest.param('a: # c\n1', 'a: # c\n→1\n', id='a line comment after the colon'),
		pytest.param('a: # c\n{b: 1}', 'a: # c\n→{b: 1}\n', id='an object on one line after a comment that follows the colon'),
		pytest.param('a: # c\n{\nb: 1}', 'a: # c\n→{\n→→b: 1\n→}\n', id='an object after a comment that follows the colon'),
		pytest.param('a:\n# c\n1', 'a:\n→# c\n→1\n', id='a comment on its own line after the colon'),
		pytest.param('a: /* x\ny */ {b: 1}', 'a: /* x\ny */ {b: 1}\n', id='a line break inside a block comment after the colon does not move the value'),
		pytest.param('/* a\n   b */ key: 1', '/* a\n   b */ key: 1\n', id='the lines of a block comment keep their indentation'),
		pytest.param('/* a\n\n\n\n   b   \n*/\nkey: 1', '/* a\n\n   b\n*/\nkey: 1\n', id='runs of blank lines in a block comment collapse to one'),
		pytest.param('# x \xa0\t\na: 1', '# x \xa0\na: 1\n', id='only spaces and tabs are trailing whitespace in a comment'),
		pytest.param('a: 1 #\ufeff', 'a: 1 #\ufeff\n', id='a byte order mark in a comment is content'),
		pytest.param(
			"pool: {\n    description: '''\n        one\n          two\n\n        ''',\n}",
			"pool: {\n→description:\n→→'''\n→→one\n→→  two\n\n→→'''\n}\n",
			id='a block string begins on the line after its key, and its delimiters and content line up',
		),
		pytest.param("a: [\n'''\nx\n'''\n]", "a: [\n→'''\n→x\n→'''\n]\n", id='a block string in an array has the indentation of the item'),
		pytest.param("a:\n\t'''\n\tx\n\t'''", "a:\n→'''\n→x\n→'''\n", id='a block string on the line after its key is already formatted'),
		pytest.param("a: '''\n\tx  \n\t'''", "a:\n→'''\n→x  \n→'''\n", id='trailing whitespace in a block string is content'),
		pytest.param('a: [\n1,\n# One\n\n# Two\n2,\n]', 'a: [\n→1\n→# One\n\n→# Two\n→2\n]\n', id='a blank line between comments is kept'),
		pytest.param('# One\n\n# Two\na: 1', '# One\n\n# Two\na: 1\n', id='a blank line between comments at the top is kept'),
		pytest.param('# Head\n[1, # One\n2] # Tail', '# Head\n[\n→1 # One\n→2\n] # Tail\n', id='a top-level array with comments'),
		pytest.param('a: [1,\n2] /* t */\nb: 3', 'a: [\n→1\n→2\n] /* t */\nb: 3\n', id='a comment after the closing bracket stays on its line'),
		pytest.param('a: [1,\n/* one\n  two */\n2]', 'a: [\n→1\n→/* one\n  two */\n→2\n]\n', id='a multi-line block comment inside an array'),
		pytest.param("a: # c\n\t'''\n\tx\n\t'''", "a: # c\n→'''\n→x\n→'''\n", id='a block string after a comment that follows the colon'),
		pytest.param("a: /* c */ '''\n\tx\n\t'''", "a: /* c */\n→'''\n→x\n→'''\n", id='a block string after a block comment on the line of the colon goes on its own line'),
		pytest.param("a: '''\n  x\n   \n  y\n  '''", "a:\n→'''\n→x\n   \n→y\n→'''\n", id='a blank line inside a block string is left as it is'),
		pytest.param('[\n\n# c\n1]', '[\n→# c\n→1\n]\n', id='no blank line before a comment directly inside a bracket'),
		pytest.param(
			'a: [\n\t1,\n\t2,\n]\nb: {\n\tc: 1,\n\td: [3, 4,],\n}',
			'a: [\n→1\n→2\n]\nb: {\n→c: 1\n→d: [3, 4]\n}\n',
			id='every comma in a container that spans lines is removed, and a trailing comma on one line',
		),
		pytest.param('[1, 2\n3,\n4,]', '[\n→1\n→2\n→3\n→4\n]\n', id='commas and line breaks mixed'),
		pytest.param('a: [\n\t1\n\t2\n]\nb: {\n\tc: 1 # c\n\td: 2\n}', 'a: [\n→1\n→2\n]\nb: {\n→c: 1 # c\n→d: 2\n}\n', id='a document without commas is already formatted'),
		pytest.param('{\na: 1\nb: 2\n}', '{\n→a: 1\n→b: 2\n}\n', id='members on their own lines without commas'),
	],
)
def test_format(text: str, expected: str) -> None:
	"""Each case is the input and the expected output, with `→` for a tab to keep the layout readable."""
	formatted = soml.format(text)
	assert formatted.replace('\t', '→') == expected
	assert canonical(formatted) == canonical(text)
	assert soml.format(formatted) == formatted


def test_format_accepts_bytes() -> None:
	assert soml.format(b'a:1') == 'a: 1\n'


def test_an_invalid_document_raises_the_error_of_loads() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Unexpected “b”'):
		soml.format('a: b')


@pytest.mark.parametrize('text', ['[1\n,\n2]', '{a: 1\n, b: 2}'])
def test_a_comma_at_the_start_of_a_line_raises(text: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='A comma must be on the same line as the item before it'):
		soml.format(text)


def test_a_value_finer_than_a_microsecond_is_formatted() -> None:
	assert soml.format('a: [1ns,\n2026-09-19T14:00:00.123456789Z]') == 'a: [\n\t1ns\n\t2026-09-19T14:00:00.123456789Z\n]\n'

	with pytest.raises(soml.SOMLDecodeError, match='finer than a microsecond'):
		soml.loads('a: 1ns')

	with pytest.raises(soml.SOMLDecodeError, match='finer than a microsecond'):
		tree.parse('a: 1ns')


def test_formatting_takes_linear_time() -> None:
	def measure(count: int) -> float:
		text = ''.join(f'key{index}: [{index}, /* c */ {index}] # note\n' for index in range(count))
		return best_time(lambda: soml.format(text), runs=3)

	measure(100)
	assert measure(16_000) / measure(2000) < 24


@pytest.mark.parametrize('template', ['a: 1{comments}\nb: 2', '[1,{comments}\n2]'], ids=['after a member', 'after a comma'])
def test_formatting_many_block_comments_on_one_line_before_an_item_takes_linear_time(template: str) -> None:
	def measure(count: int) -> float:
		text = template.format(comments=' /* c */' * count)
		return best_time(lambda: soml.format(text), runs=3)

	measure(1000)
	assert measure(80_000) / measure(10_000) < 24


def test_a_value_after_a_comment_on_the_line_after_its_key_is_one_level_deeper() -> None:
	assert soml.format('a:\n/* c */ {\nb: 1}\n') == 'a:\n\t/* c */ {\n\t\tb: 1\n\t}\n'
