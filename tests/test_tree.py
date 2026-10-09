import collections
import enum
import inspect
import math
import pickle
import time
import typing
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import best_time, typed
from hypothesis import given, settings
from hypothesis import strategies as st

import soml
from soml import tree

CONFORMANCE = Path(__file__).parent / 'conformance' / 'valid'

DOCUMENT = """\
# The edge service.
name: 'api-gateway'
port: 0x1F90  # Hex, on purpose.
postgres: {host: 'db.internal'}
pool: {
	# Minimum.
	min: 2,
	max: 16, # Maximum.
}
tags: ['a', 'b', 'c']
# Trailer.
"""


def valid_documents() -> list[bytes]:
	"""Every valid corpus file that `loads()` accepts, including the canonical and formatted forms."""
	documents = []

	for path in sorted(CONFORMANCE.rglob('*.soml')):
		data = path.read_bytes()

		try:
			soml.loads(data)
		except soml.SOMLDecodeError:
			continue

		documents.append(data)

	return documents


VALID_DOCUMENTS = valid_documents()


def edited(text: str, edit: Callable[[tree.DocumentNode], object]) -> str:
	document = tree.parse(text)
	edit(document)
	return tree.unparse(document)


def find(document: tree.DocumentNode, key: str) -> tree.MemberNode:
	# The top-level member with the key, so that a test can change its key or its value node.
	member = next((member for member in document.body.members if member.key.value == key), None) if isinstance(document.body, tree.ObjectNode) else None
	assert member is not None
	return member


def object_value(member: tree.MemberNode) -> tree.ObjectNode:
	assert isinstance(member.value, tree.ObjectNode)
	return member.value


def array_value(member: tree.MemberNode) -> tree.ArrayNode:
	assert isinstance(member.value, tree.ArrayNode)
	return member.value


def new_member(key: str, value: soml.Serializable) -> tree.MemberNode:
	node = tree.from_value({key: value})
	assert isinstance(node, tree.ObjectNode)
	return node.members[0]


def test_every_valid_corpus_document_prints_its_source_exactly() -> None:
	assert len(VALID_DOCUMENTS) > 700

	for data in VALID_DOCUMENTS:
		document = tree.parse(data)
		assert tree.unparse(document) == data.decode()
		assert document.to_value() == soml.loads(data)


def test_parse_raises_the_same_error_as_loads() -> None:
	with pytest.raises(soml.SOMLDecodeError) as tree_error:
		tree.parse('a: 1\nb: nope\n')

	with pytest.raises(soml.SOMLDecodeError) as loads_error:
		soml.loads('a: 1\nb: nope\n')

	assert (tree_error.value.msg, tree_error.value.pos) == (loads_error.value.msg, loads_error.value.pos)


def test_parse_rejects_a_value_finer_than_a_microsecond() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='finer than a microsecond'):
		tree.parse('a: 1ns\nb: 2\n')


def test_parse_accepts_bytes() -> None:
	assert tree.unparse(tree.parse(b'a: 1\n')) == 'a: 1\n'


def test_nodes() -> None:
	document = tree.parse("'b c': [1, 2.5, 0b101, \"x\", '''\n\tblock\n\t''', true, null, 2026-09-19T14:00:00+02:00, 90m] # Note.\n")
	assert isinstance(document.body, tree.ObjectNode)
	assert document.body.braced is False
	member = document.body.members[0]
	assert (member.key.value, member.key.style) == ('b c', 'literal')
	elements = array_value(member).elements
	assert elements[0] == tree.IntegerNode(1) or isinstance(elements[0], tree.IntegerNode)
	assert [type(element).__name__ for element in elements] == ['IntegerNode', 'FloatNode', 'IntegerNode', 'StringNode', 'StringNode', 'BooleanNode', 'NullNode', 'InstantNode', 'DurationNode']
	assert isinstance(elements[2], tree.IntegerNode)
	assert (elements[2].value, elements[2].radix) == (5, 2)
	assert isinstance(elements[3], tree.StringNode)
	assert (elements[3].style, elements[3].block) == ('escaped', False)
	assert isinstance(elements[4], tree.StringNode)
	assert (elements[4].value, elements[4].style, elements[4].block) == ('block', 'literal', True)
	assert isinstance(elements[7], tree.InstantNode)
	assert elements[7].value == datetime(2026, 9, 19, 12, tzinfo=UTC)
	assert isinstance(elements[8], tree.DurationNode)
	assert elements[8].value == timedelta(minutes=90)
	assert len(document.comments) == 1
	start, end = document.comments[0].range
	assert document.source[start:end] == '# Note.'
	assert document.comments[0] == tree.Comment(' Note.', 'line', (start, end))


def test_ranges_point_at_the_source() -> None:
	text = 'port: 8080 # The default\n'
	document = tree.parse(text)
	member = find(document, 'port')
	assert member.value.range == (6, 10)
	assert member.range == (0, 10)
	assert member.key.range == (0, 4)
	assert document.comments[0].range == (11, 24)
	assert text[11:24] == '# The default'
	assert document.position(6) == (1, 7)
	assert document.position(len(text)) == (2, 1)


def test_every_range_holds_its_text_and_nests_its_children() -> None:
	for data in VALID_DOCUMENTS:
		document = tree.parse(data)
		text = document.source
		assert document.range == (0, len(text))

		for node in tree.walk(document):
			if node.range is None:
				continue

			assert text[node.range[0] : node.range[1]], node

			for child in getattr(node, 'members', []) or getattr(node, 'elements', []) or [getattr(node, 'key', None), getattr(node, 'value', None)]:
				if getattr(child, 'range', None) is not None:
					assert child.range[0] >= node.range[0], (node, child)
					assert child.range[1] <= node.range[1], (node, child)


def test_tokens() -> None:
	document = tree.parse('{a: 0, \'b\': [1, 2026-09-19T14:00:00Z, 1s, true, -infinity, "x"]}')
	assert [(token.kind, token.value) for token in document.tokens] == [
		('punctuator', '{'),
		('bare_key', 'a'),
		('punctuator', ':'),
		('integer', '0'),
		('punctuator', ','),
		('string', "'b'"),
		('punctuator', ':'),
		('punctuator', '['),
		('integer', '1'),
		('punctuator', ','),
		('instant', '2026-09-19T14:00:00Z'),
		('punctuator', ','),
		('duration', '1s'),
		('punctuator', ','),
		('keyword', 'true'),
		('punctuator', ','),
		('float', '-infinity'),
		('punctuator', ','),
		('string', '"x"'),
		('punctuator', ']'),
		('punctuator', '}'),
	]


def test_change_a_scalar_keeps_everything_else() -> None:
	assert edited(DOCUMENT, lambda document: setattr(find(document, 'name').value, 'value', 'edge')) == DOCUMENT.replace("'api-gateway'", "'edge'")


def test_change_an_int_keeps_its_radix() -> None:
	def change(document: tree.DocumentNode) -> None:
		for key in 'abc':
			node = find(document, key).value
			assert isinstance(node, tree.IntegerNode)
			node.value = 9

	assert edited('a: 0x1F90\nb: 0o644\nc: 0b1\n', change) == 'a: 0x9\nb: 0o11\nc: 0b1001\n'


def test_a_negative_int_falls_back_to_decimal() -> None:
	assert edited('a: 0xFF\n', lambda document: setattr(find(document, 'a').value, 'value', -1)) == 'a: -1\n'


def test_replace_a_node_writes_it_canonically() -> None:
	assert edited(DOCUMENT, lambda document: setattr(find(document, 'port'), 'value', tree.from_value(9090))) == DOCUMENT.replace('0x1F90', '9090')


def test_a_literal_string_becomes_escaped_when_it_must() -> None:
	assert edited("a: 'x'\n", lambda document: setattr(find(document, 'a').value, 'value', "it's")) == 'a: "it\'s"\n'


def test_an_escaped_string_stays_escaped() -> None:
	assert edited('a: "x"\n', lambda document: setattr(find(document, 'a').value, 'value', 'y')) == 'a: "y"\n'


def test_a_changed_block_string_is_written_on_one_line() -> None:
	assert edited("a: '''\n\tline one\n\tline two\n\t''' # Note.\n", lambda document: setattr(find(document, 'a').value, 'value', 'one\ntwo')) == 'a: "one\\ntwo" # Note.\n'


def test_a_block_string_on_the_line_after_its_key_moves_up_when_it_is_no_longer_a_block_string() -> None:
	assert edited("a:\n\t'''\n\tline\n\t'''\n", lambda document: setattr(find(document, 'a').value, 'block', False)) == "a: 'line'\n"


def test_an_unchanged_value_keeps_its_spelling_next_to_a_change() -> None:
	text = "a: [0xFF, 1_000, 90m, 2026-09-19T14:00:00+02:00, '''\n\tx\n\t''', 'y']\n"
	assert edited(text, lambda document: setattr(array_value(find(document, 'a')).elements[5], 'value', 'z')) == text.replace("'y'", "'z'")


def test_rename_a_key() -> None:
	assert edited(DOCUMENT, lambda document: setattr(find(document, 'name').key, 'value', 'title')) == DOCUMENT.replace("name: 'api-gateway'", "title: 'api-gateway'")


def test_rename_a_key_to_one_that_needs_quotes() -> None:
	assert edited('a: 1\n', lambda document: setattr(find(document, 'a').key, 'value', 'a b')) == "'a b': 1\n"


def test_rename_a_key_to_one_with_a_dot_quotes_it() -> None:
	assert edited('a: 1\n', lambda document: setattr(find(document, 'a').key, 'value', 'a.b')) == "'a.b': 1\n"


def test_remove_the_first_member_of_a_braced_object_keeps_its_comment_line() -> None:
	assert edited(DOCUMENT, lambda document: object_value(find(document, 'pool')).members.pop(0)) == DOCUMENT.replace('\tmin: 2,\n', '')


def test_remove_the_last_member_takes_its_end_of_line_comment() -> None:
	assert edited(DOCUMENT, lambda document: object_value(find(document, 'pool')).members.pop()) == DOCUMENT.replace('\tmax: 16, # Maximum.\n', '')


@pytest.mark.parametrize('text', ['  /* c */ a: 1\nb: 2\n', '/* x\n    y */ a: 1\nb: 2\n'])
def test_an_edit_keeps_the_first_member_after_a_comment_on_its_line(text: str) -> None:
	document = tree.parse(text)
	document.set('b', 3)
	assert tree.unparse(document) == text.replace('b: 2', 'b: 3')

	document.remove('b')
	assert tree.unparse(document) == text.replace('b: 2\n', '')


def test_remove_a_top_level_member() -> None:
	assert edited(DOCUMENT, lambda document: document.body.members.pop(1) if isinstance(document.body, tree.ObjectNode) else None) == DOCUMENT.replace('port: 0x1F90  # Hex, on purpose.\n', '')


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		# A member that remains keeps its own indentation when the first member goes, and the line of the removed member goes with its indentation, as in soml-js.
		('  a: 1\n\tb: 2\n', 'a', '\tb: 2\n'),
		('a: 1\n  b: 2\n', 'a', '  b: 2\n'),
		('  a: 1\n\t\tb: 2\n', 'a', '\t\tb: 2\n'),
		('\ta: 1\n  b: 2\n', 'a', '  b: 2\n'),
		('  a: 1\nb: 2\n', 'a', 'b: 2\n'),
		('{\n\t\ta: 1\n\tb: 2\n}', 'a', '{\n\tb: 2\n}'),
		('{\n\t# c\n\t\ta: 1\n\tb: 2\n}', 'a', '{\n\t# c\n\tb: 2\n}'),
		('{/* c */ a: 1\n\tb: 2}', 'a', '{\n\tb: 2}'),
		('{ a: 1\n\tb: 2}', 'a', '{\n\tb: 2}'),
		('a: [\n\t\t1,\n\t2\n]\n', ('a', 0), 'a: [\n\t2\n]\n'),
		# A member on the line of the removed first member takes that line's indentation.
		('{\n  a: 1, b: 2\n c: 3\n}', 'a', '{\n  b: 2\n c: 3\n}'),
		# A reordered member keeps its own indentation, and the indentation of the first line goes with the member that left it.
	],
)
def test_a_removal_keeps_the_indentation_of_the_lines_that_remain(text: str, path: str | tuple[str, int], expected: str) -> None:
	document = tree.parse(text)
	document.remove(path)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('  c: 2.5\n  b: true\n', '  b: true\n  c: 2.5\n'),
		('# h\n  c: 2.5\n  b: true\n', '# h\n  b: true\n  c: 2.5\n'),
		('\n  c: 2.5\n  b: true\n\n', '\n  b: true\n  c: 2.5\n\n'),
		('  c: 2.5\nb: true\n', 'b: true\n  c: 2.5\n'),
		('c: 2.5\n  b: true\n', '  b: true\nc: 2.5\n'),
	],
)
def test_a_reorder_keeps_the_indentation_of_the_lines_that_remain(text: str, expected: str) -> None:
	document = tree.parse(text)

	def reverse(document: tree.DocumentNode) -> None:
		assert isinstance(document.body, tree.ObjectNode)
		document.body.members.reverse()

	reverse(document)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'edits', 'expected'),
	[
		# A member the program made that takes the first line keeps the indentation it is given, and the indentation of the line that the removed member held goes with it.
		('  a: 1\n\tb: 2\n', ['remove a', 'set c', 'reverse'], '\tc: 3\n\tb: 2\n'),
		('  a: 1\n\tb: 2\n', ['set c', 'reverse'], '\tc: 3\n\tb: 2\n  a: 1\n'),
		# A member the program made is given the indentation of the members it joins.
		('  c: 1\n', ['set a', 'reverse'], '  a: 9\n  c: 1\n'),
		('  a: 1\n', ['remove a', 'set b'], '  b: 9\n'),
	],
)
def test_a_member_the_program_made_takes_its_own_indentation(text: str, edits: list[str], expected: str) -> None:
	document = tree.parse(text)

	for edit in edits:
		if edit == 'reverse':
			assert isinstance(document.body, tree.ObjectNode)
			document.body.members.reverse()
		elif edit.startswith('remove '):
			document.remove(edit.removeprefix('remove '))
		elif edit.startswith('set '):
			document.set(edit.removeprefix('set '), 3 if edit[4:] == 'c' else 9)

	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		# A new member goes on a line of its own, and the spaces and tabs at the end of the line before it stay, as soml-js keeps them.
		('a: 1 ', 'a: 1 \nzz: 9'),
		('a: 1\t\t', 'a: 1\t\t\nzz: 9'),
		('a: [1, 2]  ', 'a: [1, 2]  \nzz: 9'),
		('a: 1  \n', 'a: 1  \nzz: 9\n'),
		('a: 1 # c ', 'a: 1 # c \nzz: 9'),
	],
)
def test_an_appended_member_keeps_the_trailing_whitespace_of_the_line_before(text: str, expected: str) -> None:
	document = tree.parse(text)
	document.set('zz', 9)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		# A removal takes one blank line next to the removed lines, and the rest of a run stays, as in the JS and Rust implementations.
		('first: 1\n\n\nsecond: 2\n', 'first', '\nsecond: 2\n'),
		('first: 1\n\n\nsecond: 2\n', 'second', 'first: 1\n\n'),
		('first: 1\n\n\n\n\nsecond: 2\n', 'first', '\n\n\nsecond: 2\n'),
		('first: 1\n\n\n\n\nsecond: 2\n', 'second', 'first: 1\n\n\n\n'),
		('x: 1\n\n \t\ny: 2\n', 'x', ' \t\ny: 2\n'),
		('first: 1\n\n\nmiddle: 2\n\n\nlast: 3\n', 'middle', 'first: 1\n\n\n\nlast: 3\n'),
		('first: 1\n\n\nmiddle: 2\n\n\nlast: 3\n', 'first', '\nmiddle: 2\n\n\nlast: 3\n'),
		('first: 1\n\n\nmiddle: 2\n\n\nlast: 3\n', 'last', 'first: 1\n\n\nmiddle: 2\n\n'),
		('a: [\n\t1\n\n\n\t2\n]\n', ('a', 0), 'a: [\n\n\t2\n]\n'),
		('a: [\n\t1\n\n\n\t2\n]\n', ('a', 1), 'a: [\n\t1\n\n]\n'),
		('a: {\n\tx: 1\n\n\n\ty: 2\n}\n', ('a', 'x'), 'a: {\n\n\ty: 2\n}\n'),
		# One blank line still goes, as the conformance suite fixes.
		('first: 1\n\nsecond: 2\n', 'first', 'second: 2\n'),
		('first: 1\n\nsecond: 2\n', 'second', 'first: 1\n'),
		('a: [\n\t1\n\n\t2\n]\n', ('a', 0), 'a: [\n\t2\n]\n'),
	],
)
def test_a_removal_takes_one_blank_line_of_a_run(text: str, path: str | tuple[str, int], expected: str) -> None:
	document = tree.parse(text)
	document.remove(path)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize('braced', [False, True])
def test_removal_keeps_comment_paragraphs_with_long_mixed_blank_lines(*, braced: bool) -> None:
	prefix = '# Header\u2028é😀\n' + ('{\n' if braced else '')
	suffix = '}\n' if braced else ''
	blank_lines = ' \t\n\t \n\n' * 1000
	before = f'{prefix}first: 1\n# Retained\xa0\n{blank_lines}'
	after = f'last: 3\n{suffix}'
	document = tree.parse(f'{before}removed: 2\n\n{after}')

	assert document.remove('removed')
	assert tree.unparse(document) == before + after


@pytest.mark.parametrize('braced', [False, True])
def test_removing_the_first_member_drops_only_one_following_blank_line(*, braced: bool) -> None:
	prefix = '# Header\u2028é😀\n\n' + ('{\n' if braced else '')
	suffix = '}\n' if braced else ''
	blank_lines = '\t \n\n' * 1000
	after = f'# Retained\xa0\nlast: 3\n{suffix}'
	document = tree.parse(f'{prefix}removed: 1\n \t\n{blank_lines}{after}')

	assert document.remove('removed')
	assert tree.unparse(document) == prefix + blank_lines + after


def test_remove_every_top_level_member_writes_an_empty_object() -> None:
	assert edited(DOCUMENT, lambda document: document.body.members.clear() if isinstance(document.body, tree.ObjectNode) else None) == '# The edge service.\n{}\n# Trailer.\n'


def test_remove_from_an_inline_array() -> None:
	assert edited(DOCUMENT, lambda document: array_value(find(document, 'tags')).elements.pop(1)) == DOCUMENT.replace("['a', 'b', 'c']", "['a', 'c']")
	assert edited(DOCUMENT, lambda document: array_value(find(document, 'tags')).elements.pop()) == DOCUMENT.replace("['a', 'b', 'c']", "['a', 'b']")
	assert edited('a: [1]\n', lambda document: array_value(find(document, 'a')).elements.pop()) == 'a: []\n'


def test_add_to_a_multi_line_object() -> None:
	assert edited(DOCUMENT, lambda document: object_value(find(document, 'pool')).members.append(new_member('idle', 5))) == DOCUMENT.replace(
		'\tmax: 16, # Maximum.\n', '\tmax: 16, # Maximum.\n\tidle: 5\n'
	)


def test_add_to_a_multi_line_object_without_a_trailing_comma() -> None:
	# A line break separates the new member from the one before it, so neither gets a comma, as in the JS implementation.
	assert edited('a: {\n\tb: 1\n}\n', lambda document: object_value(find(document, 'a')).members.append(new_member('c', 2))) == 'a: {\n\tb: 1\n\tc: 2\n}\n'
	# The closing brace follows the member before it on its line, so the new member goes on that line too.
	assert edited('{\n\ta: 1}\n', lambda document: document.body.members.append(new_member('b', 2)) if isinstance(document.body, tree.ObjectNode) else None) == '{\n\ta: 1, b: 2}\n'


def test_add_to_an_inline_container() -> None:
	assert edited(DOCUMENT, lambda document: array_value(find(document, 'tags')).elements.append(tree.from_value('d'))) == DOCUMENT.replace("['a', 'b', 'c']", "['a', 'b', 'c', 'd']")
	assert edited('a: {b: 1}\n', lambda document: object_value(find(document, 'a')).members.insert(0, new_member('c', 2))) == 'a: {c: 2, b: 1}\n'
	# An empty container that gets members is written on several lines, as canonical form writes it.
	assert edited('a: {}\n', lambda document: object_value(find(document, 'a')).members.append(new_member('b', 2))) == 'a: {\n\tb: 2\n}\n'


def test_a_new_child_goes_on_the_line_of_the_child_before_it_when_something_follows_it_there() -> None:
	assert edited('[\n\t1, 2\n]', lambda document: document.body.elements.insert(1, tree.from_value(9)) if isinstance(document.body, tree.ArrayNode) else None) == '[\n\t1, 9, 2\n]'
	assert edited('{a: 1,\n\tb: 2}', lambda document: document.body.members.insert(0, new_member('z', 9)) if isinstance(document.body, tree.ObjectNode) else None) == '{z: 9, a: 1,\n\tb: 2}'


def test_add_a_top_level_member() -> None:
	assert edited(DOCUMENT, lambda document: document.body.members.append(new_member('debug', value=True)) if isinstance(document.body, tree.ObjectNode) else None) == DOCUMENT.replace(
		'# Trailer.', 'debug: true\n# Trailer.'
	)
	# A document without a final line break keeps it that way.
	assert edited('a: 1', lambda document: document.body.members.append(new_member('b', 2)) if isinstance(document.body, tree.ObjectNode) else None) == 'a: 1\nb: 2'


def test_add_a_container_value_indents_it() -> None:
	assert edited('a: {\n\tb: 1,\n}\n', lambda document: object_value(find(document, 'a')).members.append(new_member('c', {'d': [1]}))) == 'a: {\n\tb: 1,\n\tc: {\n\t\td: [\n\t\t\t1\n\t\t]\n\t}\n}\n'


def test_reorder_members_moves_their_comments() -> None:
	text = 'a: 1 # One.\n# About b.\nb: 2\n'

	def reverse(document: tree.DocumentNode) -> None:
		assert isinstance(document.body, tree.ObjectNode)
		document.body.members.reverse()

	assert edited(text, reverse) == '# About b.\nb: 2\na: 1 # One.\n'


def test_a_new_document() -> None:
	document = tree.DocumentNode(tree.ObjectNode([new_member('a', 1)], braced=False))
	assert tree.unparse(document) == 'a: 1\n'
	assert tree.unparse(tree.DocumentNode(tree.ArrayNode([tree.IntegerNode(1)]))) == '[\n\t1\n]\n'


def test_a_new_body_keeps_the_header() -> None:
	assert edited('# Header.\na: 1\n', lambda document: setattr(document, 'body', tree.from_value([1]))) == '# Header.\n[\n\t1\n]\n'


def test_a_node_moved_from_another_document_keeps_its_text() -> None:
	other = tree.parse('b: 0xFF\n')

	def move(document: tree.DocumentNode) -> None:
		assert isinstance(document.body, tree.ObjectNode)
		assert isinstance(other.body, tree.ObjectNode)
		document.body.members.append(other.body.members[0])

	assert edited('a: 1\n', move) == 'a: 1\nb: 0xFF\n'


@pytest.mark.parametrize(
	('text', 'edit', 'message'),
	[
		('a: 1\n', lambda document: document.body.members.append(new_member('a', 2)), 'Duplicate key a'),
		('a: 1\n', lambda document: setattr(document.body.members[0].value, 'value', 2**63), 'outside the 64-bit range'),
	],
)
def test_an_edit_that_makes_an_invalid_document_raises(text: str, edit: Callable[[tree.DocumentNode], object], message: str) -> None:
	with pytest.raises(ValueError, match=message):
		edited(text, edit)


def test_a_circular_tree_raises() -> None:
	array = tree.ArrayNode()
	array.elements.append(array)
	document = tree.DocumentNode(array)

	with pytest.raises(ValueError, match='nested more than 100 levels deep, or it is circular'):
		tree.unparse(document)

	with pytest.raises(ValueError, match='nested more than 100 levels deep, or it is circular'):
		document.to_value()

	with pytest.raises(ValueError, match='circular'):
		list(tree.walk(array))


def test_comments_are_read_only() -> None:
	document = tree.parse('a: 1 # Note.\n')
	document.comments.clear()
	assert tree.unparse(document) == 'a: 1 # Note.\n'


def test_from_value() -> None:
	node = tree.from_value({'a b': [1, 2.5, 'x', None, True, datetime(2026, 1, 1, tzinfo=UTC), timedelta(seconds=1)]})
	assert isinstance(node, tree.ObjectNode)
	assert node.members[0].key.style == 'literal'
	assert node.to_value() == {'a b': [1, 2.5, 'x', None, True, datetime(2026, 1, 1, tzinfo=UTC), timedelta(seconds=1)]}
	assert tree.unparse(tree.DocumentNode(node)) == "{\n\t'a b': [\n\t\t1\n\t\t2.5\n\t\t'x'\n\t\tnull\n\t\ttrue\n\t\t2026-01-01T00:00:00Z\n\t\t1s\n\t]\n}\n"
	node.braced = False
	assert tree.unparse(tree.DocumentNode(node)).startswith("'a b': [\n\t1\n")


@pytest.mark.parametrize(
	('value', 'error'),
	[
		(float('nan'), ValueError),
		(2**63, ValueError),
		(datetime(2026, 1, 1), ValueError),  # noqa: DTZ001 # A naive datetime is the point.
		(object(), TypeError),
		({1: 'a'}, TypeError),
	],
)
def test_from_value_raises_the_errors_of_dumps(value: object, error: type[Exception]) -> None:
	with pytest.raises(error):
		tree.from_value(value)  # type: ignore[call-overload]

	with pytest.raises(error):
		soml.dumps([value])  # type: ignore[list-item]


def test_from_value_refuses_a_circular_value() -> None:
	value: list[object] = []
	value.append(value)

	with pytest.raises(ValueError, match=r'^Cannot serialize a circular structure$'):
		tree.from_value(value)  # type: ignore[arg-type]


def test_document_from_value() -> None:
	value: dict[str, soml.Serializable] = {'name': 'api', 'b': {'c': [1, 'x']}, 'a': []}
	document = tree.DocumentNode.from_value(value)
	assert tree.unparse(document) == "name: 'api'\nb: {\n\tc: [\n\t\t1\n\t\t'x'\n\t]\n}\na: []\n"
	assert document.to_value() == value
	assert tree.unparse(tree.DocumentNode.from_value({})) == '{}\n'
	assert tree.unparse(tree.DocumentNode.from_value([1, {'a': 1}])) == '[\n\t1\n\t{\n\t\ta: 1\n\t}\n]\n'


def test_set_on_a_document_from_value() -> None:
	document = tree.DocumentNode.from_value({'a': 1})
	document.set('b', 2)
	document.set(('c', 'd'), 3)
	assert tree.unparse(document) == 'a: 1\nb: 2\nc: {\n\td: 3\n}\n'

	# `set()` checks its result through `unparse()`, which works on a document that was not parsed.
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members.append(tree.MemberNode(tree.KeyNode('a'), tree.from_value(1)))

	with pytest.raises(ValueError, match='Duplicate key a'):
		document.set('e', 4)

	assert 'e' not in document


@pytest.mark.parametrize(
	('value', 'error', 'message'),
	[
		(1, TypeError, "The top-level value must be a dict or a list, because a document is a collection, not 'int'"),
		(date(2026, 9, 19), TypeError, "not 'date'"),
		('a', TypeError, 'not .str.'),
		(None, TypeError, 'not .NoneType.'),
		(float('nan'), TypeError, 'not .float.'),
		({1: 'a'}, TypeError, 'Object keys must be str'),
		({'a': float('nan')}, ValueError, 'NaN'),
		([2**63], ValueError, '64-bit'),
	],
)
def test_document_from_value_raises_the_errors_of_dumps(value: object, error: type[Exception], message: str) -> None:
	with pytest.raises(error, match=message):
		tree.DocumentNode.from_value(value)  # type: ignore[arg-type]

	with pytest.raises(error, match=message):
		soml.dumps(value)  # type: ignore[arg-type]


def commented_member(key: str, value: soml.Serializable, comments: list[str]) -> tree.MemberNode:
	member = new_member(key, value)
	member.comments = comments
	return member


def test_member_comments_in_a_new_document() -> None:
	document = tree.DocumentNode.from_value({'b': {'c': 1}})
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members.insert(0, commented_member('a', 1, ['The a.', '', 'More.']))
	object_value(document.body.members[1]).members.append(commented_member('d', 2, ['The d.']))
	assert tree.unparse(document) == '# The a.\n#\n# More.\na: 1\nb: {\n\tc: 1\n\t# The d.\n\td: 2\n}\n'


def test_member_comments_in_an_object_inside_an_array() -> None:
	array = tree.ArrayNode([tree.ObjectNode([commented_member('a', 1, ['The a.'])])])
	assert tree.unparse(tree.DocumentNode(array)) == '[\n\t{\n\t\t# The a.\n\t\ta: 1\n\t}\n]\n'


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('x: 1\n', 'x: 1\n# New.\nn: 1\n'),
		('x: 1 # One.', 'x: 1 # One.\n# New.\nn: 1'),
		('x: 1\n# Trailer.', 'x: 1\n# New.\nn: 1\n# Trailer.'),
		('x: {\n\ta: 1\n}\n', 'x: {\n\ta: 1\n\t# New.\n\tn: 1\n}\n'),
		('x: {}\n', 'x: {\n\t# New.\n\tn: 1\n}\n'),
		('x: {\n\t# Add here.\n}\n', 'x: {\n\t# Add here.\n\t# New.\n\tn: 1\n}\n'),
	],
)
def test_member_comments_on_a_new_member_in_a_parsed_object(text: str, expected: str) -> None:
	def add(document: tree.DocumentNode) -> None:
		node = document.get('x')
		parent = node if isinstance(node, tree.ObjectNode) and node.braced else document.body
		assert isinstance(parent, tree.ObjectNode)
		parent.members.append(commented_member('n', 1, ['New.']))

	assert edited(text, add) == expected


@pytest.mark.parametrize(
	('text', 'edit', 'message'),
	[
		('x: 1\n', lambda document: document.body.members[0].comments.append('Note.'), 'Cannot write the comments of the member x, because it is from the source'),
		(
			'x: {a: 1}\n',
			lambda document: document.get('x').members.append(commented_member('n', 1, ['New.'])),
			'Cannot write the comments of the new member n, because it goes on the line of the text before it',
		),
		('x: {\n\ta: 1}\n', lambda document: document.get('x').members.append(commented_member('n', 1, ['New.'])), 'goes on the line of the text before it'),
		('x: {a: 1}\n', lambda document: document.get('x').members.insert(0, commented_member('n', 1, ['New.'])), 'goes on the line of the text before it'),
		('x: {/* c */}\n', lambda document: document.get('x').members.append(commented_member('n', 1, ['New.'])), 'goes on the line of the text before it'),
		('x: [1]\n', lambda document: document.get('x').elements.append(tree.ObjectNode([commented_member('n', 1, ['New.'])])), 'goes on the line of the text before it'),
		('x: 1\n', lambda document: document.body.members.append(commented_member('n', 1, ['One\nm: 2'])), 'A comment of a member cannot hold a line break'),
		('x: 1\n', lambda document: document.body.members.append(commented_member('n', 1, ['One\rTwo'])), 'carriage return'),
	],
)
def test_member_comments_errors(text: str, edit: Callable[[tree.DocumentNode], object], message: str) -> None:
	document = tree.parse(text)
	edit(document)

	with pytest.raises(ValueError, match=message):
		tree.unparse(document)


def test_member_comments_with_a_line_break_in_a_new_document() -> None:
	with pytest.raises(ValueError, match='cannot hold a line break'):
		tree.unparse(tree.DocumentNode(tree.ObjectNode([commented_member('a', 1, ['One\nb: 2'])], braced=False)))


@pytest.mark.parametrize('comments', ['The port.', 'x'], ids=['a sentence', 'one character'])
def test_member_comments_that_are_a_str_rather_than_a_list_raise_a_type_error(comments: str) -> None:
	member = new_member('port', 8080)
	member.comments = comments  # type: ignore[assignment]

	with pytest.raises(TypeError, match=r'^The comments of a member are a list of str, one for each line, not a str$'):
		tree.unparse(tree.DocumentNode(tree.ObjectNode([member], braced=False)))

	document = tree.parse('a: 1\n')
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members.append(member)

	with pytest.raises(TypeError, match=r'^The comments of a member are a list of str'):
		tree.unparse(document)


def test_a_document_pickles() -> None:
	document = tree.parse(DOCUMENT)
	assert tree.unparse(pickle.loads(pickle.dumps(document))) == DOCUMENT  # noqa: S301 # The data was just made.


type Edit = Callable[[], None]


def containers(node: tree.Node) -> list[tree.ObjectNode | tree.ArrayNode]:
	found: list[tree.ObjectNode | tree.ArrayNode] = []

	match node:
		case tree.DocumentNode(body=body):
			found.extend(containers(body))
		case tree.ObjectNode(members=members):
			found.append(node)

			for member in members:
				found.extend(containers(member.value))
		case tree.ArrayNode(elements=elements):
			found.append(node)

			for element in elements:
				found.extend(containers(element))
		case _:
			pass

	return found


SCALAR_VALUES = st.one_of(
	st.text(alphabet=st.characters(exclude_characters='\r', exclude_categories=['Cs']), max_size=10),
	st.integers(min_value=-(2**63), max_value=2**63 - 1),
	st.floats(allow_nan=False),
	st.booleans(),
	st.none(),
	st.timedeltas(min_value=timedelta(days=-100_000), max_value=timedelta(days=100_000)),
	st.datetimes(timezones=st.just(UTC)),
)


# More examples than the default profile, because the edits are random sequences. The larger count of the profile applies, so that the thorough profile runs these too.
@settings(max_examples=max(300, settings().max_examples))
@given(st.sampled_from(VALID_DOCUMENTS), st.data())
def test_random_edits_print_the_value_that_the_tree_holds(data: bytes, draw: st.DataObject) -> None:
	"""Change, remove, or add random children, and check that what `unparse()` writes reads back as what the tree holds."""
	document = tree.parse(data)
	new_key_count = 0

	for _ in range(draw.draw(st.integers(min_value=1, max_value=4))):
		container = draw.draw(st.sampled_from(containers(document)))
		children: list[tree.MemberNode] | list[tree.ValueNode] = container.members if isinstance(container, tree.ObjectNode) else container.elements
		operation = draw.draw(st.sampled_from(['change', 'remove', 'add']))

		if operation == 'remove' and children and not (container is document.body and len(children) == 1):
			children.pop(draw.draw(st.integers(min_value=0, max_value=len(children) - 1)))
		elif operation == 'change' and children:
			index = draw.draw(st.integers(min_value=0, max_value=len(children) - 1))
			new_value = tree.from_value(draw.draw(SCALAR_VALUES))

			if isinstance(container, tree.ObjectNode):
				container.members[index].value = new_value
			else:
				container.elements[index] = new_value
		else:
			new_key_count += 1
			index = draw.draw(st.integers(min_value=0, max_value=len(children)))
			value = draw.draw(SCALAR_VALUES)

			if isinstance(container, tree.ObjectNode):
				# A key that no document in the corpus uses, so that it cannot collide.
				container.members.insert(index, new_member(f'new-key-{new_key_count}', value))
			else:
				container.elements.insert(index, tree.from_value(value))

	text = tree.unparse(document)
	assert soml.loads(text) == document.to_value()


@settings(max_examples=max(300, settings().max_examples))
@given(st.sampled_from(VALID_DOCUMENTS), st.data())
def test_changing_one_scalar_changes_only_its_text(data: bytes, draw: st.DataObject) -> None:
	document = tree.parse(data)
	source = data.decode()
	scalars = [
		element
		for container in containers(document)
		for element in (container.elements if isinstance(container, tree.ArrayNode) else [member.value for member in container.members])
		if isinstance(element, tree.IntegerNode | tree.StringNode | tree.BooleanNode)
	]

	if not scalars:
		return

	scalar = draw.draw(st.sampled_from(scalars))
	assert scalar.range is not None
	start, end = scalar.range
	replacement: str

	match scalar:
		case tree.IntegerNode():
			scalar.value = 42
			replacement = {16: '0x2A', 8: '0o52', 2: '0b101010'}.get(scalar.radix, '42')
		case tree.StringNode():
			scalar.value = 'new'
			replacement = '"new"' if scalar.style == 'escaped' else "'new'"
		case tree.BooleanNode():
			scalar.value = not scalar.value
			replacement = 'true' if scalar.value else 'false'

	# A block string that begins on the line after its key is replaced by a value on the key's line.
	key_ends = [
		member.key.range[1] for container in containers(document) if isinstance(container, tree.ObjectNode) for member in container.members if member.value is scalar and member.key.range is not None
	]

	if isinstance(scalar, tree.StringNode) and scalar.block and key_ends and not source[key_ends[0] + 1 : start].strip(' \t\n'):
		start = key_ends[0] + 1
		replacement = f' {replacement}'

	assert tree.unparse(document) == source[:start] + replacement + source[end:]


def test_an_edit_deep_in_nested_objects_takes_linear_time() -> None:
	# Each member computed its value's text twice, so the time doubled at each level.
	depth = 99
	document = tree.parse('a: ' + '{a: ' * (depth - 1) + '1' + '}' * (depth - 1) + '\n')
	node: tree.Node = document.body

	while isinstance(node, tree.ObjectNode):
		node = node.members[0].value

	assert isinstance(node, tree.IntegerNode)
	node.value = 2
	start = time.process_time()
	text = tree.unparse(document)
	assert time.process_time() - start < 1
	assert text == 'a: ' + '{a: ' * (depth - 1) + '2' + '}' * (depth - 1) + '\n'


def test_an_edit_in_a_wide_inline_array_takes_linear_time() -> None:
	# Each item looked back to the start of its line for its indentation.
	count = 50_000
	document = tree.parse('[' + ', '.join(map(str, range(count))) + ']\n')
	assert isinstance(document.body, tree.ArrayNode)
	first = document.body.elements[0]
	assert isinstance(first, tree.IntegerNode)
	first.value = -1
	start = time.process_time()
	text = tree.unparse(document)
	assert time.process_time() - start < 2
	assert text == '[-1, ' + ', '.join(map(str, range(1, count))) + ']\n'


def test_a_brace_less_body_moved_under_a_key_gets_braces() -> None:
	old = tree.parse('a: 1 # One.\nb: 2\n')
	document = tree.parse('config: null\n')
	find(document, 'config').value = old.body
	assert tree.unparse(document) == 'config: {\n\ta: 1\n\tb: 2\n}\n'
	assert soml.loads(tree.unparse(document)) == {'config': {'a': 1, 'b': 2}}


def test_a_brace_less_body_moved_into_an_array_gets_braces() -> None:
	old = tree.parse('a: 1\n')
	document = tree.parse('[1]\n')
	assert isinstance(document.body, tree.ArrayNode)
	document.body.elements.append(old.body)
	assert soml.loads(tree.unparse(document)) == [1, {'a': 1}]


def test_a_plain_value_where_a_node_goes_raises_a_type_error() -> None:
	document = tree.parse('port: 8080\n')
	find(document, 'port').value = 9090  # type: ignore[assignment]

	with pytest.raises(TypeError, match=r"Expected a node, not 'int'\. Use `from_value\(\)`"):
		tree.unparse(document)


@pytest.mark.parametrize('text', ['a: 1', 'a: 1\n', 'a: 1\n\nb: [\n\t2,\n]\n# End.', "'日本語😀': 'é😀'\n", "text: 'one\u0085two\u2028three\u2029four'\n"])
def test_position_matches_the_error_positions(text: str) -> None:
	document = tree.parse(text)

	for offset in range(len(text) + 1):
		error = soml.SOMLDecodeError('x', text, offset)
		assert document.position(offset) == (error.lineno, error.colno)


def test_from_value_has_a_precise_type() -> None:
	# Checked by mypy: no narrowing is needed for the node that a mapping makes.
	member = tree.from_value({'debug': False}).members[0]
	assert member.key.value == 'debug'
	assert tree.from_value(1).radix == 10
	assert tree.from_value(True).value is True  # noqa: FBT003 # A value, not a flag.
	assert tree.from_value([1]).elements[0].to_value() == 1


def test_a_float_node_may_hold_an_int() -> None:
	document = tree.parse('ratio: 1.5\n')
	node = find(document, 'ratio').value
	assert isinstance(node, tree.FloatNode)
	node.value = 2
	assert tree.unparse(document) == 'ratio: 2.0\n'


def test_to_value_refuses_a_duplicate_key() -> None:
	document = tree.parse('a: {b: 1}\n')
	assert isinstance(document.body, tree.ObjectNode)
	other = tree.parse('a: {c: 2}\n')
	assert isinstance(other.body, tree.ObjectNode)
	document.body.members.append(other.body.members[0])

	with pytest.raises(ValueError, match=r'^Duplicate key a$'):
		document.to_value()


def test_to_value_of_negative_zero_is_zero_as_loads_reads_it() -> None:
	document = tree.parse('a: 1.5\n')
	node = document.get('a')
	assert isinstance(node, tree.FloatNode)
	node.value = -0.0
	value = document.to_value()
	assert isinstance(value, dict)
	assert isinstance(value['a'], float)
	assert math.copysign(1, value['a']) == 1
	assert tree.unparse(document) == 'a: 0.0\n'


@pytest.mark.parametrize('key', ['a\rb', '\ud800'])
def test_from_value_checks_keys_as_dumps_does(key: str) -> None:
	with pytest.raises(ValueError, match='Cannot serialize a key'):
		tree.from_value({key: 1})


def test_from_value_refuses_two_keys_that_are_the_same_string() -> None:
	class Pairs(Mapping[str, int]):
		def __getitem__(self, key: str) -> int:
			return 1

		def __iter__(self) -> Iterator[str]:
			return iter(['a', 'a'])

		def __len__(self) -> int:
			return 2

	with pytest.raises(ValueError, match="two keys that are the same string: 'a'"):
		tree.from_value(Pairs())


def test_unparse_needs_a_document() -> None:
	with pytest.raises(TypeError, match='Expected a DocumentNode'):
		tree.unparse(tree.from_value({}))  # type: ignore[arg-type]


def test_annotations_can_be_inspected() -> None:
	for name in ['StringNode', 'IntegerNode', 'KeyNode', 'Comment', 'Token', 'DocumentNode', 'from_value']:
		typing.get_type_hints(getattr(tree, name))
		inspect.signature(getattr(tree, name))


def test_a_float_node_with_an_int_reads_and_prints_as_a_float() -> None:
	document = tree.DocumentNode(tree.from_value({'a': 1.5}))
	assert isinstance(document.body, tree.ObjectNode)
	node = document.body.members[0].value
	assert isinstance(node, tree.FloatNode)
	node.value = 5
	assert typed(document.to_value()) == typed(soml.loads(tree.unparse(document))) == {'a': ('float', 5.0)}


def test_a_float_node_with_an_int_too_large_for_a_float_raises_a_value_error() -> None:
	node = tree.FloatNode(10**400)

	with pytest.raises(ValueError, match='too large to be a finite float'):
		tree.unparse(tree.DocumentNode(tree.ArrayNode([node])))


def test_number_subclasses_print_and_read_as_their_value() -> None:
	class OddFloat(float):
		__slots__ = ()

		def __float__(self) -> float:
			return 99.0

	class OddInt(int):
		__slots__ = ()

		def __format__(self, format_spec: str) -> str:
			return 'ZZ'

	document = tree.parse('a: 1.5\nb: 0x1\n')
	float_node = find(document, 'a').value
	int_node = find(document, 'b').value
	assert isinstance(float_node, tree.FloatNode)
	assert isinstance(int_node, tree.IntegerNode)
	float_node.value = OddFloat(2.5)
	int_node.value = OddInt(31)
	assert tree.unparse(document) == 'a: 2.5\nb: 0x1F\n'
	assert typed(document.to_value()) == typed(soml.loads(tree.unparse(document)))


def test_the_overloads_of_from_value_can_be_inspected() -> None:
	for overload in typing.get_overloads(tree.from_value):
		typing.get_type_hints(overload)


def test_a_str_subclass_with_its_own_format_prints_and_reads_as_its_value() -> None:
	class Color(str, enum.Enum):  # noqa: UP042 # Not `StrEnum` on purpose: this pattern overrides `__format__`, which is the point.
		__slots__ = ()

		RED = 'red'

	document = tree.parse("a: 'x'\n'b c': 1\n")
	value = find(document, 'a').value
	assert isinstance(value, tree.StringNode)
	value.value = Color.RED
	find(document, 'b c').key.value = Color.RED
	assert tree.unparse(document) == "a: 'red'\n'red': 1\n"
	assert typed(document.to_value()) == typed(soml.loads(tree.unparse(document))) == {'a': ('str', 'red'), 'red': ('int', 1)}


def test_a_changed_escaped_key_stays_escaped() -> None:
	document = tree.parse('"a": 1\n')
	find(document, 'a').key.value = 'b'
	assert tree.unparse(document) == '"b": 1\n'


def test_the_to_value_error_is_sanitized() -> None:
	document = tree.parse('"\\u{202e}abc": 1\n')
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members.append(document.body.members[0])

	with pytest.raises(ValueError, match='Duplicate key') as error_info:
		document.to_value()

	assert '\u202e' not in str(error_info.value)


def test_from_value_has_the_depth_limit_of_dumps() -> None:
	deepest: list[object] = []

	for _ in range(99):
		deepest = [deepest]

	tree.from_value(deepest)  # type: ignore[arg-type]
	soml.dumps(deepest)  # type: ignore[arg-type]

	with pytest.raises(ValueError, match='nested more than 100 levels deep'):
		tree.from_value([deepest])  # type: ignore[list-item]

	with pytest.raises(ValueError, match='nested more than 100 levels deep'):
		soml.dumps([deepest])  # type: ignore[list-item]


def test_string_styles_are_read_from_the_source() -> None:
	document = tree.parse('''[\'a\', "a", \'\'\'\n\ta\n\t\'\'\', """\n\ta\n\t"""]\n''')
	assert isinstance(document.body, tree.ArrayNode)
	styles = [(element.style, element.block) for element in document.body.elements if isinstance(element, tree.StringNode)]
	assert styles == [('literal', False), ('escaped', False), ('literal', True), ('escaped', True)]


def test_zero_keeps_its_radix() -> None:
	def change(document: tree.DocumentNode) -> None:
		for key in 'abc':
			node = find(document, key).value
			assert isinstance(node, tree.IntegerNode)
			node.value = 0

	assert edited('a: 0xFF\nb: 0o7\nc: 0b1\n', change) == 'a: 0x0\nb: 0o0\nc: 0b0\n'


def test_walk_yields_every_node_in_source_order() -> None:
	document = tree.parse("a: [1, {c: 'x'}]\nd: null\n")
	nodes = list(tree.walk(document))
	assert [type(node).__name__ for node in nodes] == [
		'DocumentNode',
		'ObjectNode',
		'MemberNode',
		'KeyNode',
		'ArrayNode',
		'IntegerNode',
		'ObjectNode',
		'MemberNode',
		'KeyNode',
		'StringNode',
		'MemberNode',
		'KeyNode',
		'NullNode',
	]
	starts = [node.range[0] for node in nodes[1:] if node.range is not None]
	assert starts == sorted(starts)


def test_walk_starts_at_any_node() -> None:
	document = tree.parse('a: [1, 2]\n')
	array = find(document, 'a').value
	assert [type(node).__name__ for node in tree.walk(array)] == ['ArrayNode', 'IntegerNode', 'IntegerNode']
	null = tree.NullNode()
	assert list(tree.walk(null)) == [null]


def test_walk_does_not_recurse() -> None:
	array = tree.ArrayNode()
	node = array

	for _ in range(5000):
		child = tree.ArrayNode()
		node.elements.append(child)
		node = child

	assert sum(1 for _ in tree.walk(array)) == 5001


def test_walk_raises_for_a_circular_tree() -> None:
	holder = tree.ArrayNode()
	holder.elements.append(holder)

	with pytest.raises(ValueError, match='circular'):
		list(tree.walk(holder))

	# A node shared in two places is not circular.
	shared = tree.from_value(1)
	parent = tree.ArrayNode([shared, shared])
	assert sum(1 for _ in tree.walk(parent)) == 3


def test_instants_and_durations_are_exact_and_in_utc() -> None:
	class PlainInstant(datetime):
		pass

	class PlainDuration(timedelta):
		pass

	offset = timezone(timedelta(hours=2))
	node = tree.from_value({'a': PlainInstant(2026, 1, 1, 2, tzinfo=offset), 'b': PlainDuration(seconds=5)})
	document = tree.DocumentNode(node)
	assert typed(document.to_value()) == typed(soml.loads(tree.unparse(document))) == {'a': ('datetime', datetime(2026, 1, 1, tzinfo=UTC)), 'b': ('timedelta', timedelta(seconds=5))}
	instant = node.members[0].value
	assert isinstance(instant, tree.InstantNode)
	assert instant.value.tzinfo is UTC


@pytest.mark.parametrize('offset', [timedelta(seconds=2589), timedelta(seconds=-1, microseconds=1), timedelta(hours=-23, minutes=-59, seconds=-59)], ids=['seconds', 'microseconds', 'almost a day'])
def test_an_instant_with_an_offset_that_is_not_whole_minutes_is_written_as_dumps_writes_it(offset: timedelta) -> None:
	value = {'a': datetime(2026, 9, 19, 14, tzinfo=timezone(offset))}
	document = tree.DocumentNode.from_value(value)
	assert tree.unparse(document) == soml.dumps(value)
	# The same instant, also set into a parsed document.
	parsed = tree.parse('a: 1\n')
	parsed.set('a', value['a'])
	assert tree.unparse(parsed) == soml.dumps(value)


def test_a_boolean_node_with_a_value_that_is_not_a_bool_raises() -> None:
	document = tree.DocumentNode(tree.ArrayNode([tree.BooleanNode('false')]))  # type: ignore[arg-type]

	with pytest.raises(TypeError, match="A BooleanNode holds a bool, not 'str'"):
		tree.unparse(document)

	with pytest.raises(TypeError, match="A BooleanNode holds a bool, not 'str'"):
		document.to_value()


@pytest.mark.parametrize(
	('text', 'value', 'message'),
	[
		('a: 1\n', 'hi', r'An IntegerNode holds an int, not \'str\''),
		('a: 1\n', True, r'An IntegerNode holds an int, not \'bool\''),
		("a: 'hi'\n", 123, r'A StringNode holds a str, not \'int\''),
		('a: 1.5\n', 'hi', r'A FloatNode holds a float, not \'str\''),
		('a: 1.5\n', True, r'A FloatNode holds a float, not \'bool\''),
		('a: true\n', 1, r'A BooleanNode holds a bool, not \'int\''),
		('a: 2020-01-01T00:00:00Z\n', 'hi', r'An InstantNode holds a datetime, not \'str\''),
		('a: 2026-11-01\n', 'hi', r'A DateNode holds a date, not \'str\''),
		('a: 1s\n', 'hi', r'A DurationNode holds a timedelta, not \'str\''),
	],
	ids=['int as str', 'int as bool', 'str as int', 'float as str', 'float as bool', 'bool as int', 'instant as str', 'date as str', 'duration as str'],
)
def test_a_node_with_a_value_of_the_wrong_type_raises_a_type_error(text: str, value: object, message: str) -> None:
	document = tree.parse(text)
	node = document.get('a')
	assert node is not None
	node.value = value  # type: ignore[union-attr]

	with pytest.raises(TypeError, match=message):
		tree.unparse(document)

	with pytest.raises(TypeError, match=message):
		document.to_value()


@pytest.mark.parametrize(
	('text', 'value', 'message'),
	[
		('a: 1\n', 2**63, 'Cannot serialize an integer outside the 64-bit range'),
		("a: 'hi'\n", '\ud800', 'Cannot serialize a string containing a lone surrogate'),
		("a: 'hi'\n", 'a\rb', 'Cannot serialize a string containing a carriage return'),
		('a: 1.0\n', float('nan'), 'Cannot serialize NaN'),
	],
	ids=['int range', 'lone surrogate', 'carriage return', 'nan'],
)
def test_to_value_refuses_a_scalar_that_dumps_refuses(text: str, value: object, message: str) -> None:
	document = tree.parse(text)
	node = document.get('a')
	assert node is not None
	node.value = value  # type: ignore[union-attr]

	with pytest.raises(ValueError, match=message):
		document.to_value()

	with pytest.raises(ValueError, match=message):
		tree.unparse(document)


def test_to_value_refuses_a_key_that_dumps_refuses() -> None:
	document = tree.parse('a: 1\n')
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members[0].key.value = 'a\rb'

	with pytest.raises(ValueError, match='Cannot serialize a key'):
		document.to_value()

	with pytest.raises(ValueError, match='Cannot serialize a key'):
		tree.unparse(document)


def test_to_value_refuses_a_child_that_is_not_a_node() -> None:
	document = tree.parse('[1, 2]\n')
	assert isinstance(document.body, tree.ArrayNode)
	document.body.elements[0] = 123  # type: ignore[call-overload]

	with pytest.raises(TypeError, match='Expected a node'):
		document.to_value()


def test_a_comment_that_is_not_a_str_raises_a_type_error() -> None:
	document = tree.DocumentNode.from_value({'a': 1})
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members[0].comments = [123]  # type: ignore[list-item]

	with pytest.raises(TypeError, match=r'^A comment of a member is a str, not \'int\'$'):
		tree.unparse(document)


def test_to_value_refuses_a_body_that_is_not_a_node() -> None:
	document = tree.parse('a: 1\n')
	document.body = 'hi'  # type: ignore[assignment]

	with pytest.raises(TypeError, match='Expected a node'):
		document.to_value()


def test_an_object_node_with_braced_that_is_not_a_bool_raises() -> None:
	document = tree.parse('a: 1\n')
	assert isinstance(document.body, tree.ObjectNode)
	document.body.braced = 'yes'  # type: ignore[assignment]

	with pytest.raises(TypeError, match=r'^An ObjectNode holds a bool for braced, not \'str\'$'):
		tree.unparse(document)


@pytest.mark.parametrize(('text', 'style'), [('a: 1\n', 'foo'), ('a: 1\n', ''), ("a: 'hi'\n", 'foo'), ("a: 'hi'\n", 'bare')], ids=['foo key', 'empty key', 'foo string', 'bare for a string'])
def test_a_node_with_a_style_that_is_not_valid_raises(text: str, style: object) -> None:
	document = tree.parse(text)
	node = document.get('a')
	assert isinstance(node, (tree.IntegerNode, tree.StringNode))
	assert isinstance(document.body, tree.ObjectNode)
	target = node if isinstance(node, tree.StringNode) else document.body.members[0].key
	target.style = style  # type: ignore[assignment]

	with pytest.raises(ValueError, match=r'The style of a (String|Key)Node'):
		tree.unparse(document)


@pytest.mark.parametrize(('value', 'radix'), [(16, 3), (16, 0), (-16, 3), (16, '16'), (16, 10.0), (16, True)], ids=['3', '0', 'a negative value', 'a str', 'a float', 'a bool'])
def test_an_integer_node_with_a_radix_that_is_not_one_of_the_four_raises_a_value_error(value: int, radix: object) -> None:
	document = tree.parse('a: 0x10\n')
	node = document.get('a')
	assert isinstance(node, tree.IntegerNode)
	node.value = value
	node.radix = radix  # type: ignore[assignment]

	with pytest.raises(ValueError, match=f'^The radix of an IntegerNode is 2, 8, 10, or 16, not {radix!r}$'):
		tree.unparse(document)

	# `set()` checks the document by printing it, so it raises the same error and undoes the change.
	with pytest.raises(ValueError, match='The radix of an IntegerNode'):
		document.set('b', 1)

	assert 'b' not in document


def test_parsing_many_block_comments_on_one_line_takes_linear_time() -> None:
	# Each run of block comments that ends a line is checked once, not once for each of its comments.
	def measure(count: int) -> float:
		text = 'a: [1, ' + '/**/' * count + '\n2]\nb: 1 ' + '/**/' * count + '\n'
		return best_time(lambda: tree.parse(text), runs=3)

	measure(100)
	assert measure(16_000) / measure(2000) < 24


@pytest.mark.parametrize('offset', [-1, 11])
def test_position_refuses_an_offset_outside_the_source(offset: int) -> None:
	with pytest.raises(ValueError, match='is outside the source, which has 10 characters'):
		tree.parse('a: 1\nb: 2\n').position(offset)


def test_an_unchanged_tree_prints_its_source_without_reading_it_again() -> None:
	text = 'a: 1 # Note.\n'
	document = tree.parse(text)
	assert tree.unparse(document) is document.source


@pytest.mark.parametrize(
	('text', 'braced', 'expected'),
	[
		pytest.param("# Header.\nname: 'api'\nport: 8080\n", True, "# Header.\n{\n\tname: 'api'\n\tport: 8080\n}\n", id='braces added keep the final line break'),
		pytest.param("name: 'api'\nport: 8080", True, "{\n\tname: 'api'\n\tport: 8080\n}\n", id='braces added end the document with a line break as a new document does'),
		pytest.param('{\n\ta: 1\n}\n', False, 'a: 1\n', id='braces removed keep the final line break'),
		pytest.param('# Header.\n{a: 1}\n# Trailer.\n', False, '# Header.\na: 1\n# Trailer.\n', id='braces removed keep the comments outside them'),
	],
)
def test_changing_braced_on_the_body_prints_it_fresh_and_ends_with_a_line_break(text: str, braced: bool, expected: str) -> None:  # noqa: FBT001 # A parameter.
	document = tree.parse(text)
	assert isinstance(document.body, tree.ObjectNode)
	document.body.braced = braced
	assert tree.unparse(document) == expected
	assert soml.loads(expected) == soml.loads(text)


def test_from_value_refuses_a_user_string_as_dumps_does() -> None:
	with pytest.raises(TypeError, match=r"^Object of type 'UserString' is not SOML serializable$"):
		tree.from_value({'a': collections.UserString('x')})

	with pytest.raises(TypeError, match=r"^The top-level value must be a dict or a list, because a document is a collection, not 'UserString'$"):
		tree.DocumentNode.from_value(collections.UserString('x'))
