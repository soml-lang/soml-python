"""Formatting preserves spelling and comment placement across nested layouts."""

import tracemalloc

import pytest

import soml
from soml import tree
from soml._format import reindent_block_string
from soml._parser import MAX_DEPTH


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('{"a\\u{3a}b": [0x00FF,0b0001,1_000,1.00e2,], \'é😀\': {a: [],b: {}}}', '{"a\\u{3a}b": [0x00FF, 0b0001, 1_000, 1.00e2], \'é😀\': {a: [], b: {}}}\n'),
		('[ [1,2], [3,4] ] /* , [5] */', '[[1, 2], [3, 4]] /* , [5] */\n'),
		('[ [1,2] /* a, */ , /* b, */ [3,4], ]', '[[1, 2] /* a, */, /* b, */ [3, 4]]\n'),
		('[\n[1,2]\n\n[3,4] # , [5]\n]', '[\n\t[1, 2]\n\n\t[3, 4] # , [5]\n]\n'),
		("/* before */ {'': [1,2]} /* after */", "/* before */ {'': [1, 2]} /* after */\n"),
		('a: [/* , */]\nb: { /* , */ }\nc: [1,2]', 'a: [/* , */]\nb: {/* , */}\nc: [1, 2]\n'),
	],
)
def test_inline_normalization_preserves_spelling_and_surrounding_comments(text: str, expected: str) -> None:
	assert soml.format(text) == expected
	assert soml.loads(expected) == soml.loads(text)
	assert soml.format(expected) == expected


def test_colon_comments_and_block_strings_at_the_nesting_limit() -> None:
	text = 'key: # next\n{\n' * (MAX_DEPTH - 1) + "key: # text\n'''\ncontent\n'''\n" + '}\n' * (MAX_DEPTH - 1)
	formatted = soml.format(text)
	assert '\n' + '\t' * (2 * MAX_DEPTH - 1) + 'content\n' in formatted
	assert soml.loads(formatted) == soml.loads(text)
	assert soml.format(formatted) == formatted


def test_multiline_block_comment_keeps_unicode_line_content_and_closing_indentation() -> None:
	text = '/*\n\n \t\nhello\u2028world  \t\n\n\nlast\n  \t*/\na: [1,2]'
	expected = '/*\n\nhello\u2028world\n\nlast\n  \t*/\na: [1, 2]\n'
	assert soml.format(text) == expected
	assert soml.format(expected) == expected


def test_reindenting_a_long_block_string_reuses_its_line_buffer() -> None:
	line = 'x' * 80
	text = "'''\n" + f'    {line}\n' * 20_000 + "    '''"
	tracemalloc.start()
	try:
		result = reindent_block_string(text, '\t')
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert result == "'''\n" + f'\t{line}\n' * 20_000 + "\t'''"
	assert peak < len(text) * 3


def test_tree_printing_preserves_reordered_and_imported_source_children() -> None:
	document = tree.parse('[0x1, 0x2, 0x3]')
	assert isinstance(document.body, tree.ArrayNode)
	first, _, last = document.body.elements
	imported = tree.parse('[0xF]').get(0)
	assert imported is not None
	document.body.elements[:] = [last, imported, first]
	assert tree.unparse(document) == '[0x3, 0xF, 0x1, ]'
	assert document.to_value() == [3, 15, 1]


def test_tree_printing_allows_shared_source_containers_without_a_cycle() -> None:
	document = tree.parse('[[1], [2]]')
	assert isinstance(document.body, tree.ArrayNode)
	document.body.elements[1] = document.body.elements[0]
	assert tree.unparse(document) == '[[1], [1]]'
	assert document.to_value() == [[1], [1]]
