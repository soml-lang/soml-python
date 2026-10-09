"""
Tests for `DocumentNode.get()`, `.set()`, `.remove()`, `in`, and `del`. The layout cases are the ones of the Rust implementation, soml-rust, which gives the same text for each.
"""

import copy
import random
import re
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import best_time, typed, value_paths
from hypothesis import given, settings
from hypothesis import strategies as st

import soml
from soml import tree

TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable

ROOT = Path(__file__).parent / 'conformance' / 'valid'


@pytest.mark.parametrize(
	('text', 'path', 'value', 'expected'),
	[
		pytest.param("# Head\nport: 0x1F90 # The default.\nname: 'x'\n", 'port', 8080, "# Head\nport: 8080 # The default.\nname: 'x'\n", id='replace a value and keep its comments'),
		pytest.param('{a: 1, b: 2}', 'b', 'two', "{a: 1, b: 'two'}", id='replace in a one-line object'),
		pytest.param('a: [1, 2, 3]', ('a', 1), 9.5, 'a: [1, 9.5, 3]', id='replace an array item'),
		pytest.param('server: {\n\tport: 1,\n}\n', ('server', 'port'), {'a': 1, 'b': 2}, 'server: {\n\tport: {\n\t\ta: 1\n\t\tb: 2\n\t},\n}\n', id='a new object on indented lines'),
		pytest.param('a: 1\n', 'list', [1, 2], 'a: 1\nlist: [\n\t1\n\t2\n]\n', id='a new array on indented lines'),
		pytest.param('a: 1\n', 'empty', [], 'a: 1\nempty: []\n', id='a new empty array'),
		pytest.param('a: 1\n', 'b', 2, 'a: 1\nb: 2\n', id='a top-level member on a new line'),
		pytest.param('a: 1', 'b', 2, 'a: 1\nb: 2', id='no line break is added at the end'),
		pytest.param('a: 1 # note\n# end\n', 'b', 2, 'a: 1 # note\nb: 2\n# end\n', id='a new member goes before the comments at the end'),
		pytest.param("'the name': 1\n", 'a.b', 2, "'the name': 1\n'a.b': 2\n", id='a key that needs quotes'),
		pytest.param(
			"server: {\n\thost: 'a',\n}\n", ('server', 'port'), 1, "server: {\n\thost: 'a',\n\tport: 1\n}\n", id='a braced object with trailing commas keeps them, and the new member has none'
		),
		pytest.param("server: {\n\thost: 'a'\n}\n", ('server', 'port'), 1, "server: {\n\thost: 'a'\n\tport: 1\n}\n", id='a braced object without a trailing comma'),
		pytest.param("server: {host: 'a'}\n", ('server', 'port'), 1, "server: {host: 'a', port: 1}\n", id='a one-line object'),
		pytest.param('server: {}\n', ('server', 'port'), 1, 'server: {\n\tport: 1\n}\n', id='an empty object'),
		pytest.param('a: {\n\tb: {},\n}\n', ('a', 'b', 'c'), 1, 'a: {\n\tb: {\n\t\tc: 1\n\t},\n}\n', id='a nested empty object'),
		pytest.param('{\n\ta: 1,\n}\n', 'b', 2, '{\n\ta: 1,\n\tb: 2\n}\n', id='a braced document'),
		pytest.param('a: [1, 2]', ('a', 2), 3, 'a: [1, 2, 3]', id='append to a one-line array'),
		pytest.param('a: [\n\t1\n\t2]', ('a', 2), 3, 'a: [\n\t1\n\t2, 3]', id='a new item goes on the line of the item before it when the closing bracket follows it there'),
		pytest.param('a: [\n\t1,\n\t2,]', ('a', 2), 3, 'a: [\n\t1,\n\t2, 3,]', id='a new item on the line of the item before it keeps the trailing comma'),
		pytest.param('a: [\n\t1 # c\n]', ('a', 1), 2, 'a: [\n\t1 # c\n\t2\n]', id='a new item goes after the comment at the end of the line before it'),
		pytest.param('a: [\n\t1,\n]\n', ('a', 1), 2, 'a: [\n\t1,\n\t2\n]\n', id='append to a multi-line array'),
		pytest.param('a: []\n', ('a', 0), 1, 'a: [\n\t1\n]\n', id='append to an empty array'),
		pytest.param('[1]', (1,), 2, '[1, 2]', id='append to a top-level array'),
		pytest.param('x: 1\n', 'a.b', 'y', "x: 1\n'a.b': 'y'\n", id='a new key with a dot is quoted'),
		pytest.param("'a.b': 1\na: {b: 2}\n", 'a.b', 3, "'a.b': 3\na: {b: 2}\n", id='a key with a dot is one key, not a path'),
		pytest.param('a: 1\n', ('server', 'tls', 'enabled'), True, 'a: 1\nserver: {\n\ttls: {\n\t\tenabled: true\n\t}\n}\n', id='a missing parent gets braces'),
		pytest.param('{a: 1, c: 2}', ('d', 'e'), 3, '{a: 1, c: 2, d: {e: 3}}', id='a missing parent on one line gets braces on one line'),
		pytest.param('\ta: 1\n', 'b', 2, '\ta: 1\n\tb: 2\n', id='the indentation of an indented first line'),
		pytest.param('  a: {\n  }', ('a', 'b'), 2, '  a: {\n  \tb: 2\n  }', id='an empty object on two indented lines'),
		pytest.param('a: 1 # c', 'b', 2, 'a: 1 # c\nb: 2', id='a comment at the end of a file without a line break'),
		pytest.param('a: { /* c */ }', ('a', 'b'), 1, 'a: { /* c */ b: 1}', id='a comment in an empty object on one line stays, and the new member goes after it'),
		pytest.param('a: [ /* c */ ]', ('a', 0), 1, 'a: [ /* c */ 1]', id='a comment in an empty array on one line stays, and the new item goes after it'),
		pytest.param('a: [/* c */]', ('a', 0), {'b': [1]}, 'a: [/* c */ {b: [1]}]', id='a new value in an empty container on one line is on one line'),
		pytest.param('a: { /* c */\n}', ('a', 'b'), 1, 'a: { /* c */\n\tb: 1\n}', id='a comment in an empty object that spans lines stays'),
		pytest.param('a: [ ]', ('a', 0), {'b': [1]}, 'a: [\n\t{\n\t\tb: [\n\t\t\t1\n\t\t]\n\t}\n]', id='a new item in an empty array with only a space inside goes on its own line'),
		pytest.param('deps: {}', ('deps', 'a'), '1.0', "deps: {\n\ta: '1.0'\n}", id='a new member in an empty object goes on its own line'),
		pytest.param('a: [1, []]', ('a', 1, 0), 2, 'a: [1, [2]]', id='a new item in an empty array inside an array on one line stays on that line'),
		pytest.param('a: {b: 1, c: {}}', ('a', 'c', 'd'), [1], 'a: {b: 1, c: {d: [1]}}', id='a new member in an empty object inside an object on one line stays on that line'),
		pytest.param('a: [1, 2]', ('a', 2), {'b': 1, 'c': [True]}, 'a: [1, 2, {b: 1, c: [true]}]', id='a new item in an array on one line is on one line'),
		pytest.param('a: [ [], 1 ]', ('a', 0, 0), [3], 'a: [ [[3]], 1 ]', id='a new item in an empty array inside an array on one line is on one line'),
		pytest.param('[/* None yet */]', 0, 1, '[/* None yet */ 1]', id='a new item in a top-level array on one line with only a comment goes after the comment'),
		pytest.param('a: {x: 1}', ('a', 'x'), [1, {'y': 2}], 'a: {x: [1, {y: 2}]}', id='a replaced value in an object on one line is on one line'),
		pytest.param('a: {x: 1}', ('a', 'y', 'z'), 2, 'a: {x: 1, y: {z: 2}}', id='a missing parent in an object on one line is on one line'),
		pytest.param('a: 1', 'a', {'b': 1}, 'a: {\n\tb: 1\n}', id='a new object in a brace-less document spans lines'),
		pytest.param('{ }', 'b', 1, '{\n\tb: 1\n}', id='an empty braced document'),
		pytest.param("a:\n\t'''\n\tx\n\t'''\nb: 1\n", 'a', 2, 'a: 2\nb: 1\n', id='a block string that is replaced moves up to the line of its key'),
		pytest.param("x: {\n\ta:\n\t\t'''\n\t\tx\n\t\t'''\n}\n", ('x', 'a'), [1], 'x: {\n\ta: [\n\t\t1\n\t]\n}\n', id='a block string that is replaced by an array moves up to the line of its key'),
		pytest.param("a: # c\n\t'''\n\tx\n\t'''\n", 'a', 2, 'a: # c\n\t2\n', id='a block string that is replaced after a comment stays on its own line'),
		pytest.param('a: # c\n\t1\n', 'a', {'b': [1]}, 'a: # c\n\t{\n\t\tb: [\n\t\t\t1\n\t\t]\n\t}\n', id='a new object on the line after its key is indented from that line'),
		pytest.param('x: {\n\ta: # c\n\t\t1\n}\n', ('x', 'a'), [1], 'x: {\n\ta: # c\n\t\t[\n\t\t\t1\n\t\t]\n}\n', id='a new array on the line after its key is indented from that line'),
		pytest.param('a: # c\n\t[1, 2\n\t]\n', ('a', 1), {'q': 1}, 'a: # c\n\t[1, {\n\t\tq: 1\n\t}\n\t]\n', id='a new item in an array on the line after its key is indented from that line'),
		pytest.param('a: # c\n\t[1]\n', ('a', 1), {'q': 1}, 'a: # c\n\t[1, {q: 1}]\n', id='a new item in a one-line array on the line after its key is on one line'),
		pytest.param("a: # c\n\t'''\n\tx\n\t'''\n", 'a', [1], 'a: # c\n\t[\n\t\t1\n\t]\n', id='a block string that is replaced by an array after a comment is indented from its line'),
		pytest.param('a:\n\n 1\n', 'a', {'b': 1}, 'a:\n\n {\n \tb: 1\n }\n', id='a new object on a later line is indented from that line'),
		pytest.param(
			'x: {\n\ta: 1, b: 2\n}\n',
			('x', 'b'),
			{'k': [1]},
			'x: {\n\ta: 1, b: {\n\t\tk: [\n\t\t\t1\n\t\t]\n\t}\n}\n',
			id='a new object for a member that does not start its line is indented from that line',
		),
		pytest.param("a: [1,\n\t2, 'y']\n", ('a', 2), {'k': 1}, 'a: [1,\n\t2, {\n\t\tk: 1\n\t}]\n', id='a new object for an item that does not start its line is indented from that line'),
		pytest.param('{\n\ta: [1, 2\n\t]\n}\n', ('a', 1), [3], '{\n\ta: [1, [\n\t\t3\n\t]\n\t]\n}\n', id='a new array for an item on the line of its key is indented from that line'),
		pytest.param('b: []\n', ('b', 0, 'c'), 1, 'b: [\n\t{\n\t\tc: 1\n\t}\n]\n', id='a path below the index equal to the length of an array makes the item'),
		pytest.param('b: [1]\n', ('b', 1, 'c', 'd'), 1, 'b: [1, {c: {d: 1}}]\n', id='a path below the index equal to the length of an array on one line'),
		pytest.param(
			'{\n\ta: 1 # x\n\t/* t\nwo */ b: 2\n}\n',
			'c',
			1,
			'{\n\ta: 1 # x\n\t/* t\nwo */ b: 2\n\tc: 1\n}\n',
			id='a new member after a block comment over two lines is indented as the line where the comment starts',
		),
	],
)
def test_set(text: str, path: tree.Path, value: soml.Serializable, expected: str) -> None:
	document = tree.parse(text)
	document.set(path, value)
	assert tree.unparse(document) == expected
	assert typed(soml.loads(expected)) == typed(document.to_value())


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		pytest.param('a: 1\nb: 2 # About b.\nc: 3\n', 'b', 'a: 1\nc: 3\n', id='the comment at the end of the line goes with it'),
		pytest.param('{a: 1, b: 2}', 'a', '{b: 2}', id='the first member of a one-line object'),
		pytest.param('{a: 1, b: 2}', 'b', '{a: 1}', id='the last member of a one-line object'),
		pytest.param('{\n\ta: 1,\n\tb: 2,\n}\n', 'b', '{\n\ta: 1,\n}\n', id='the last member of a multi-line object'),
		pytest.param('{\n\ta: 1,\n\tb: 2,\n}\n', 'a', '{\n\tb: 2,\n}\n', id='the first member of a multi-line object'),
		pytest.param('x: {\n\ta: 1,\n}\n', ('x', 'a'), 'x: {}\n', id='the only member leaves an empty object'),
		pytest.param('a: [1, 2, 3]', ('a', 1), 'a: [1, 3]', id='an array item'),
		pytest.param('# Head\na: 1\nb: 2\n', 'a', '# Head\nb: 2\n', id='the comment lines above stay'),
		pytest.param('a: 1\n\n# Section\nb: 2\nc: 3\n', 'b', 'a: 1\n\n# Section\nc: 3\n', id='a section comment stays'),
		pytest.param('{\n\ta: 1,\n\t# Last\n\tb: 2,\n}\n', 'b', '{\n\ta: 1,\n\t# Last\n}\n', id='the comment above the last member stays'),
		pytest.param("'a.b': 1\na: {b: 2}\n", 'a.b', 'a: {b: 2}\n', id='a key with a dot is one key, not a path'),
		pytest.param('a: 1\n', 'a', '{}\n', id='the last top-level member leaves an empty object'),
		pytest.param('a: 1', 'a', '{}', id='the last top-level member of a document without a final line break leaves an empty object without one'),
		pytest.param('# head\na: 1 # c\n# tail\n', 'a', '# head\n{} # c\n# tail\n', id='the last top-level member between comment lines leaves an empty object in its place, with its comment'),
		pytest.param('{\n\ta: 1, # x\n\t/* t\nwo */ b: 2,\n}\n', 'b', '{\n\ta: 1, # x\n}\n', id='a block comment over two lines before it goes with it'),
		pytest.param('{\n\ta: 1,\n\t/* t\nwo */\n\tb: 2,\n\tc: 3,\n}\n', 'b', '{\n\ta: 1,\n\t/* t\nwo */\n\tc: 3,\n}\n', id='a block comment over two lines above it stays whole'),
		pytest.param('{a: 1 /* c */, b: 2}', 'b', '{a: 1 /* c */}', id='a comment before the comma it drops stays'),
		pytest.param('[1 /* x */, 2]', (1,), '[1 /* x */]', id='a comment before the comma of the item before'),
		pytest.param('a: 1\n/* c */ b: 2', 'a', '/* c */ b: 2', id='the next member keeps the comment in front of it'),
		pytest.param('{a: 1,\n\t/* c */ b: 2}', 'a', '{\n\t/* c */ b: 2}', id='the next member keeps its line when the first member goes'),
		pytest.param('[1, /* c */ 2]', (0,), '[/* c */ 2]', id='the next item keeps the comment in front of it'),
		pytest.param('a: 1\nb: 2 # t', 'b', 'a: 1\n', id='a comment at the end of a file without a line break'),
	],
)
def test_remove(text: str, path: tree.Path, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove(path)
	assert tree.unparse(document) == expected
	assert typed(soml.loads(expected)) == typed(document.to_value())


def test_get() -> None:
	document = tree.parse("server: {port: 0x1F90, tls: {cert: 'a'}}\nhosts: ['a', 'b']\n'a.b': 1\n")
	port = document.get(('server', 'port'))
	assert isinstance(port, tree.IntegerNode)
	assert (port.value, port.radix) == (8080, 16)
	cert = document.get(('server', 'tls', 'cert'))
	assert isinstance(cert, tree.StringNode)
	assert cert.value == 'a'
	host = document.get(('hosts', 1))
	assert isinstance(host, tree.StringNode)
	assert host.value == 'b'
	assert isinstance(document.get('a.b'), tree.IntegerNode)
	assert isinstance(document.get('server'), tree.ObjectNode)
	assert document.get(('a', 'b')) is None
	assert document.get('missing') is None
	assert document.get(('hosts', 2)) is None
	assert document.get(('hosts', 'x')) is None
	assert document.get(('server', 'port', 'x')) is None


def test_get_in_a_top_level_array() -> None:
	document = tree.parse('[{a: 1}, [2]]')
	assert isinstance(document.get((0, 'a')), tree.IntegerNode)
	assert isinstance(document.get((1, 0)), tree.IntegerNode)
	assert document.get('a') is None


def test_set_changes_the_node_that_get_returns_and_keeps_its_spelling_when_changed_in_place() -> None:
	document = tree.parse('port: 0x1F90\n')
	node = document.get('port')
	assert isinstance(node, tree.IntegerNode)
	node.value = 9090
	assert tree.unparse(document) == 'port: 0x2382\n'


@pytest.mark.parametrize(
	('path', 'message'),
	[
		(('a', 'b'), 'Cannot edit a.b, because a is not an object or an array'),
		(('b', 'x'), 'Cannot edit b.x, because b is an array, so it needs an index, not a key'),
		(('b', 5), 'Cannot edit b[5], because the array at b has 1 item. Add an item at index 1'),
		(('a', 0), 'Cannot edit a[0], because a is not an object or an array'),
		((0,), 'Cannot edit [0], because the document is an object, so it needs a key, not an index'),
		(('c', 0), 'Cannot edit c[0], because c does not exist, so it has no index 0'),
		(('server', 0), 'Cannot edit server[0], because server is an object, so it needs a key, not an index'),
		(('b', 1, 0), 'Cannot edit b[1][0], because b[1] does not exist, so it has no index 0'),
		((), 'The path must be a non-empty array of keys and array indexes'),
		(('b', -1), 'The path must be a non-empty array of keys and array indexes'),
	],
)
def test_set_refuses_a_path_that_does_not_fit(path: tree.Path, message: str) -> None:
	text = 'a: 1\nb: [1]\nserver: {port: 1}\n'
	document = tree.parse(text)

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		document.set(path, 1)

	assert tree.unparse(document) == text


@pytest.mark.parametrize('value', [float('nan'), 'a\rb', 2**63])
def test_set_refuses_a_value_that_cannot_be_written(value: object) -> None:
	document = tree.parse('a: 1\n')

	with pytest.raises(ValueError, match='Cannot serialize'):
		document.set('b', value)  # type: ignore[arg-type]

	assert tree.unparse(document) == 'a: 1\n'


def test_set_refuses_a_key_that_cannot_be_written_and_leaves_the_document_as_it_was() -> None:
	document = tree.parse('a: 1')

	with pytest.raises(ValueError, match='carriage return'):
		document.set('x\ry', 1)

	assert tree.unparse(document) == 'a: 1'


def test_set_refuses_a_change_that_makes_the_document_too_deep_and_leaves_it_as_it_was() -> None:
	# The top-level object and 98 more are 99 levels, so the leaf can become one more array but not two.
	text = 'a: {' * 98 + 'a: 1' + '}' * 98
	document = tree.parse(text)
	path = ('a',) * 99

	with pytest.raises(ValueError, match='nested more than 100 levels deep'):
		document.set(path, [[]])

	assert tree.unparse(document) == text
	document.set(path, [])
	assert soml.loads(tree.unparse(document)) == document.to_value()


@pytest.mark.parametrize(
	('path', 'message'),
	[
		(('a', 'b'), 'Cannot edit a.b, because a is not an object or an array'),
		(('b', 'x'), 'Cannot edit b.x, because b is an array, so it needs an index, not a key'),
		((0,), 'Cannot edit [0], because the document is an object, so it needs a key, not an index'),
	],
)
def test_remove_refuses_a_path_that_does_not_fit(path: tree.Path, message: str) -> None:
	document = tree.parse('a: 1\nb: [1]\n')

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		document.remove(path)


# A key of 40 characters, the most that an error message shows of one key.
LONG_KEY = 'k' + '123456789' * 4 + '012'


# The errors of soml-js `edit()` for the same edits, where `None` as the value is a removal, as `undefined` is there.
@pytest.mark.parametrize(
	('text', 'path', 'value', 'message'),
	[
		('a: 1\n', ('a', 'b'), 1, 'Cannot edit a.b, because a is not an object or an array'),
		('a: 1\n', ('a', 'b'), None, 'Cannot edit a.b, because a is not an object or an array'),
		('b: [1]\n', ('b', 'x'), None, 'Cannot edit b.x, because b is an array, so it needs an index, not a key'),
		('b: []\n', ('b', 2), 1, 'Cannot edit b[2], because the array at b has 0 items. Add an item at index 0'),
		('[1, 2]\n', (5,), 1, 'Cannot edit [5], because the document has 2 items. Add an item at index 2'),
		('[1]\n', ('x',), 1, 'Cannot edit x, because the document is an array, so it needs an index, not a key'),
		('a: 1\n', (0,), None, 'Cannot edit [0], because the document is an object, so it needs a key, not an index'),
		('a: {b: 1}\n', ('a', 0), 1, 'Cannot edit a[0], because a is an object, so it needs a key, not an index'),
		('a: {b: {c: 1}}\n', ('a', 'b', 0, 'x'), None, 'Cannot edit a.b[0].x, because a.b is an object, so it needs a key, not an index'),
		('a: {b: 1}\n', ('a', 'x', 0), 1, 'Cannot edit a.x[0], because a.x does not exist, so it has no index 0'),
		# A key with a dot is quoted, so that it is not read as two keys.
		("'a.b': 1\n", ('a.b', 'c'), 1, 'Cannot edit "a.b".c, because "a.b" is not an object or an array'),
		('b: [1]\n', ('b', 1, 'x', 0, 'y', 2), 1, 'Cannot edit b[1].x[0].y[2], because b[1].x[0].y does not exist, so it has no index 2'),
		('a: [{b: [1, 2]}]\n', ('a', 0, 'b', 7), 1, 'Cannot edit a[0].b[7], because the array at a[0].b has 2 items. Add an item at index 2'),
		('[[1]]\n', (0, 'k'), None, 'Cannot edit [0].k, because [0] is an array, so it needs an index, not a key'),
		('"x y": [1]\n', ('x y', 'z'), 1, 'Cannot edit "x y".z, because "x y" is an array, so it needs an index, not a key'),
		('a: 1\n', ('a', 'x\x1by\u200b\u2028'), 1, 'Cannot edit a."x\\u001by\\u{200b}\\u{2028}", because a is not an object or an array'),
		('a: 1\n', ('a', '\ud800'), None, 'Cannot edit a."\\ud800", because a is not an object or an array'),
		('a: 1\n', ('a', f'{LONG_KEY}0'), 1, f'Cannot edit a.{LONG_KEY}…, because a is not an object or an array'),
		('a: 1\n', (*[LONG_KEY] * 6, 0), 1, f'Cannot edit {".".join([LONG_KEY] * 4)}.{LONG_KEY[:36]}…, because {".".join([LONG_KEY] * 4)}.{LONG_KEY[:36]}… does not exist, so it has no index 0'),
		('a: 1\n', (), 1, 'The path must be a non-empty array of keys and array indexes'),
		('a: 1\n', ('a', -1), None, 'The path must be a non-empty array of keys and array indexes'),
	],
)
def test_set_and_remove_raise_the_errors_of_soml_js(text: str, path: tree.Path, value: soml.Serializable, message: str) -> None:
	document = tree.parse(text)

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		document.remove(path) if value is None else document.set(path, value)


def test_remove_reports_whether_it_removed_something() -> None:
	document = tree.parse('a: 1\nb: [1]\n')
	assert not document.remove('missing')
	assert not document.remove(('b', 3))
	assert tree.unparse(document) == 'a: 1\nb: [1]\n'


# Removing what is not there changes nothing and is not an error, as the "Editing" rules of the spec say, as long as the path does not reach a value of the wrong kind first.
@pytest.mark.parametrize(
	'path',
	[
		pytest.param('missing', id='a missing member'),
		pytest.param(('missing', 'x'), id='a missing object on the way'),
		pytest.param(('missing', 0), id='an index below a missing value'),
		pytest.param(('missing', 0, 'x', 1), id='a path of any shape below a missing value'),
		pytest.param(('b', 2), id='an index at the end'),
		pytest.param(('b', 5), id='an index past the end'),
		pytest.param(('b', 5, 'x'), id='a path below an index past the end'),
		pytest.param(('c', 'x'), id='a missing member of an object'),
		pytest.param(('c', 'x', 0), id='an index below a missing member of an object'),
		pytest.param(('d', 'x'), id='a missing member of an empty object'),
		pytest.param(('e', 0), id='an index in an empty array'),
		pytest.param(('b', 1, 'x', 0), id='a path below a missing member of an item'),
	],
)
def test_removing_what_is_not_there_changes_nothing(path: tree.Path) -> None:
	text = 'a: 1\nb: [1, {y: 2}]\nc: {y: 3}\nd: {}\ne: []\n'
	document = tree.parse(text)
	assert not document.remove(path)
	assert tree.unparse(document) == text

	with pytest.raises(KeyError):
		del document[path]

	assert tree.unparse(document) == text


def test_removing_the_same_path_twice_is_safe() -> None:
	document = tree.parse('a: 1\nb: [1, 2]\nc: {d: 3}\n')
	assert document.remove(('b', 1))
	assert not document.remove(('b', 1))
	assert document.remove(('c', 'd'))
	assert not document.remove(('c', 'd'))
	assert tree.unparse(document) == 'a: 1\nb: [1]\nc: {}\n'


# A path that reaches a value of the wrong kind before it reaches nothing does not fit the document, so a removal raises, as a set does.
@pytest.mark.parametrize(
	('path', 'message'),
	[
		(('a', 'x', 'y'), 'Cannot edit a.x.y, because a is not an object or an array'),
		(('b', 'x', 0), 'Cannot edit b.x[0], because b is an array, so it needs an index, not a key'),
		(('c', 0, 'x'), 'Cannot edit c[0].x, because c is an object, so it needs a key, not an index'),
		(('b', 0, 'x'), 'Cannot edit b[0].x, because b[0] is not an object or an array'),
		(('b', 1, 0), 'Cannot edit b[1][0], because b[1] is an object, so it needs a key, not an index'),
	],
)
def test_set_and_remove_raise_for_a_value_of_the_wrong_kind_before_the_path_leads_to_nothing(path: tree.Path, message: str) -> None:
	text = 'a: 1\nb: [1, {y: 2}]\nc: {y: 3}\n'
	document = tree.parse(text)

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		document.remove(path)

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		del document[path]

	with pytest.raises(ValueError, match=f'^{re.escape(message)}$'):
		document.set(path, 1)

	assert tree.unparse(document) == text


def test_in_tells_whether_there_is_a_value_at_a_path() -> None:
	document = tree.parse("postgres: {port: 5432}\nservers: [{port: 1}]\nname: 'x'\n")
	assert 'name' in document
	assert ('postgres', 'port') in document
	assert ('servers', 0, 'port') in document
	assert 'postgres' in document
	assert ('postgres', 'host') not in document
	assert ('servers', 1) not in document
	assert ('name', 'x') not in document
	assert ('servers', 'x') not in document


def test_in_checks_the_path_as_get_does() -> None:
	document = tree.parse('a: 1\n')

	with pytest.raises(ValueError, match='The path must be a non-empty array'):
		assert () in document

	with pytest.raises(TypeError, match='The path must be a non-empty array'):
		assert ('a', 1.5) in document  # type: ignore[operator]


def test_del_removes_as_remove_does_and_raises_key_error_when_nothing_is_there() -> None:
	document = tree.parse("name: 'api' # The service.\nport: 8080\npostgres: {host: 'a', port: 1}\n")
	del document['name']
	del document['postgres']
	assert tree.unparse(document) == 'port: 8080\n'

	with pytest.raises(KeyError, match='name'):
		del document['name']

	with pytest.raises(ValueError, match=r'^Cannot edit port\.x, because port is not an object or an array$'):
		del document['port', 'x']

	assert tree.unparse(document) == 'port: 8080\n'


@pytest.mark.parametrize('segment', [True, 1.5, None, b'a'])
def test_a_path_holds_keys_and_indices(segment: object) -> None:
	document = tree.parse('a: 1\n')

	with pytest.raises(TypeError, match=r'^The path must be a non-empty array of keys and array indexes$'):
		document.get(('a', segment))  # type: ignore[arg-type]


def test_a_string_path_is_one_key() -> None:
	document = tree.parse('abc: 1\na: {b: {c: 2}}\n')
	assert isinstance(document.get('abc'), tree.IntegerNode)


def test_replacing_an_item_keeps_the_comment_at_the_end_of_its_line() -> None:
	document = tree.parse('ports: [\n\t80, # HTTP\n\t443,\n]\n')
	document.set(('ports', 0), 8080)
	assert tree.unparse(document) == 'ports: [\n\t8080, # HTTP\n\t443,\n]\n'


def test_set_and_remove_take_linear_time_in_a_wide_document() -> None:
	def measure(count: int) -> float:
		text = ''.join(f'key{index}: {index}\n' for index in range(count))
		# A fresh document for each run, made before the timing starts.
		documents = [tree.parse(text) for _ in range(5)]

		def edit() -> None:
			document = documents.pop()
			document.set(f'key{count // 2}', 'x')
			document.remove(f'key{count // 3}')

		return best_time(edit)

	# Both sizes once first, because the first time this much memory is used is slower.
	measure(2000)
	measure(16_000)
	assert measure(16_000) / measure(2000) < 24


def documents() -> list[str]:
	"""The valid corpus documents that Python can hold, which are the starting points for random edits."""
	texts: list[str] = []

	for path in sorted(ROOT.rglob('*.soml')):
		text = path.read_bytes().decode()

		try:
			soml.loads(text)
		except soml.SOMLDecodeError:
			continue

		texts.append(text)

	return texts


DOCUMENTS = documents()


def plain_set(value: object, path: tuple[str | int, ...], new_value: object) -> None:
	"""The change that `set()` makes, on a plain value. A missing parent object is made."""
	for segment in path[:-1]:
		if isinstance(value, dict) and segment not in value:
			value[segment] = {}

		value = value[segment]  # type: ignore[index]

	if isinstance(value, list) and path[-1] == len(value):
		value.append(new_value)
	else:
		value[path[-1]] = new_value  # type: ignore[index]


def plain_remove(value: object, path: tuple[str | int, ...]) -> None:
	for segment in path[:-1]:
		value = value[segment]  # type: ignore[index]

	del value[path[-1]]  # type: ignore[attr-defined]


SCALARS = st.one_of(
	st.integers(min_value=-(2**63), max_value=2**63 - 1),
	st.floats(allow_nan=False),
	st.text(alphabet=st.characters(exclude_characters='\r', exclude_categories=['Cs']), max_size=8),
	st.booleans(),
	st.none(),
	st.timedeltas(min_value=timedelta(days=-1000), max_value=timedelta(days=1000)),
)
VALUES = st.recursive(SCALARS, lambda children: st.lists(children, max_size=3) | st.dictionaries(st.sampled_from(['a', 'b', 'x', 'new key', '1']), children, max_size=3), max_leaves=6)


# More examples than the default profile, because the edits are random sequences. The larger count of the profile applies, so that the thorough profile runs this too.
@settings(max_examples=max(400, settings().max_examples))
@given(st.sampled_from(DOCUMENTS), st.data())
def test_random_set_and_remove_match_the_same_change_on_the_plain_value(text: str, data: st.DataObject) -> None:
	"""Each change must give the value that the same change gives on the plain value, or be refused, leaving the text as it was."""
	document = tree.parse(text)
	expected: object = soml.loads(text)

	for _ in range(data.draw(st.integers(min_value=1, max_value=4))):
		paths = value_paths(expected)

		if not paths:
			break

		path = data.draw(st.sampled_from(paths))
		before = tree.unparse(document)

		if data.draw(st.booleans()):
			# A sibling of an existing path, which may not exist yet, or the path itself.
			if isinstance(path[-1], str) and data.draw(st.booleans()):
				path = (*path[:-1], data.draw(st.sampled_from(['a', 'b', 'new', 'x.y'])))
			elif isinstance(path[-1], int) and data.draw(st.booleans()):
				path = (*path[:-1], path[-1] + 1)

			new_value = data.draw(VALUES)

			try:
				document.set(path, new_value)
			except ValueError:
				assert tree.unparse(document) == before
				continue

			updated = copy.deepcopy(expected)

			try:
				plain_set(updated, path, soml.loads(soml.dumps({'v': new_value}))['v'])  # type: ignore[call-overload]
			except IndexError, KeyError, TypeError:
				pytest.fail(f'set() accepted {path!r}, which does not fit the value')

			expected = updated
		else:
			assert document.remove(path)
			plain_remove(expected, path)

		text_after = tree.unparse(document)
		assert typed(soml.loads(text_after)) == typed(expected)
		assert typed(document.to_value()) == typed(expected)


def test_get_and_in_on_every_path_of_the_corpus() -> None:
	for text in DOCUMENTS:
		document = tree.parse(text)
		value = soml.loads(text)

		for path in value_paths(value):
			node = document.get(path)
			expected: object = value
			parent: object = value

			for segment in path:
				parent = expected
				expected = expected[segment]  # type: ignore[index]

			assert path in document
			# A sibling that is not there: a key that no corpus document has, or the index past the end.
			missing = (*path[:-1], len(parent)) if isinstance(parent, list) else (*path[:-1], '\u0000 missing')
			assert missing not in document

			assert node is not None
			assert typed(node.to_value()) == typed(expected)


def test_an_edit_anywhere_in_a_formatted_document_leaves_it_formatted() -> None:
	nested: soml.Serializable = {'k': [1, {'z': 2}]}

	for text in DOCUMENTS:
		formatted = soml.format(text)
		value = soml.loads(formatted)

		for path in value_paths(value):
			document = tree.parse(formatted)
			node = document.get(path)
			# Replace the value, and add a new member or item when it is a container, also with a path below a new item.
			edits: list[tree.Path] = [path]

			if isinstance(node, tree.ObjectNode):
				edits.append((*path, 'new-key'))
			elif isinstance(node, tree.ArrayNode):
				edits += [(*path, len(node.elements)), (*path, len(node.elements), 'new-key')]

			for edit in edits:
				document = tree.parse(formatted)

				try:
					document.set(edit, nested)
				except ValueError:
					# Too deep.
					continue

				printed = tree.unparse(document)
				assert soml.format(printed) == printed, (formatted, edit)

			# Remove it, with no exception for any layout.
			document = tree.parse(formatted)
			assert document.remove(path)
			printed = tree.unparse(document)
			assert soml.format(printed) == printed, (formatted, path)
			expected = copy.deepcopy(value)
			plain_remove(expected, path)
			assert typed(soml.loads(printed)) == typed(expected), (formatted, path)


def test_an_int_is_a_path_into_a_top_level_array() -> None:
	document = tree.parse('[1, 2]')
	assert isinstance(document.get(0), tree.IntegerNode)
	document.set(2, 3)
	assert document.remove(0)
	assert tree.unparse(document) == '[2, 3]'

	with pytest.raises(ValueError, match=re.escape('Cannot edit [5], because the document has 2 items. Add an item at index 2')):
		document.set(5, 1)


@pytest.mark.parametrize(
	('text', 'edits', 'expected'),
	[
		pytest.param('x: [\n\t1, # one\n\t2, # two\n]\n', [('set', ('x', 1), 9), ('remove', ('x', 0))], 'x: [\n\t9, # two\n]\n', id='replace then remove before it'),
		pytest.param('x: [\n\t1, # one\n\t2, # two\n\t3, # three\n]\n', [('remove', ('x', 0)), ('set', ('x', 1), 9)], 'x: [\n\t2, # two\n\t9, # three\n]\n', id='remove then replace after it'),
		pytest.param('a: 1\nb: 2 # about b\n', [('remove', 'b'), ('set', 'c', 3)], 'a: 1\nc: 3\n', id='a removed member does not give its comment to a new one'),
		pytest.param('{a: 1, /* about b */ b: 2}', [('remove', 'b'), ('set', 'c', 3)], '{a: 1, c: 3}', id='a removed member does not give its block comment to a new one'),
	],
)
def test_a_replaced_item_keeps_its_own_comments_whatever_else_changes(text: str, edits: list[tuple[str, tree.Path, int]] | list[tuple[str, tree.Path]], expected: str) -> None:
	document = tree.parse(text)

	for edit in edits:
		if edit[0] == 'set':
			document.set(edit[1], edit[2])  # type: ignore[misc]
		else:
			document.remove(edit[1])

	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		pytest.param('{/* first */ a: 1, b: 2}', 'a', '{b: 2}', id='a comment in front of the first member goes with it'),
		pytest.param('x: [\n\t1, # one\n\t2, # two\n\t3 # three\n]', ('x', 2), 'x: [\n\t1, # one\n\t2, # two\n]', id='a comma before a line break stays when the last item goes'),
		# The line break after the opening bracket stays when the only member or item on its line goes, so a comment line after it stays on its own line.
		pytest.param('x: [1,\n\t# end\n]\n', ('x', 0), 'x: [\n\t# end\n]\n', id='a comment line after the only item stays on its own line'),
		pytest.param('x: [1\n\t# end\n]\n', ('x', 0), 'x: [\n\t# end\n]\n', id='a comment line after the only item without a comma stays on its own line'),
		pytest.param('x: {a: 1,\n\t# end\n}\n', ('x', 'a'), 'x: {\n\t# end\n}\n', id='a comment line after the only member stays on its own line'),
		pytest.param('[1,\n# end\n]\n', (0,), '[\n# end\n]\n', id='a comment line without indentation stays on its own line'),
		pytest.param('{a: 1, # c\n\t# end\n}\n', 'a', '{\n\t# end\n}\n', id='the comment at the end of the line of the only member goes with it'),
		pytest.param('x: [ /* c */ 1,\n\t# end\n]\n', ('x', 0), 'x: [\n\t# end\n]\n', id='the comment in front of the only item goes with it'),
		pytest.param('x: [1,\n\t/* a\n\tb */\n]\n', ('x', 0), 'x: [\n\t/* a\n\tb */\n]\n', id='a block comment over two lines after the only item stays on its own lines'),
		# A removal takes one blank line of a run, and the rest of the run stays, as in soml-js.
		pytest.param('a: [\n\t1\n\n\n\t2\n]\n', ('a', 0), 'a: [\n\n\t2\n]\n', id='one blank line of a run is left after an opening bracket'),
		pytest.param('a: [\n\t1\n\n\n\t2\n]\n', ('a', 1), 'a: [\n\t1\n\n]\n', id='one blank line of a run is left before a closing bracket'),
		pytest.param('a: [\n\t1\n\n\n\t2\n\n\n\t3\n]\n', ('a', 1), 'a: [\n\t1\n\n\n\n\t3\n]\n', id='two runs are joined by one more blank line'),
		pytest.param('a: [\n\t1\n\n\t2\n\n\n\t3\n]\n', ('a', 1), 'a: [\n\t1\n\n\n\t3\n]\n', id='the run after stays when the run before goes'),
		pytest.param('x: 1\n\n\ny: 2\n', 'x', '\ny: 2\n', id='one blank line of a run is left at the start of a document'),
		pytest.param('x: 1\n\n\ny: 2\n', 'y', 'x: 1\n\n', id='one blank line of a run is left at the end of a document'),
		pytest.param('x: 1\n\n \t\ny: 2\n', 'y', 'x: 1\n\n', id='a blank line with spaces and tabs is in the run'),
		pytest.param('# h\n\n\nx: 1\n\n\ny: 2\n', 'x', '# h\n\n\n\ny: 2\n', id='a run after a header stays'),
		pytest.param(
			'{\n\ta: 1\n\n\n\t# About b.\n\tb: 2\n\n\n\tc: 3\n}\n', 'b', '{\n\ta: 1\n\n\n\t# About b.\n\n\n\tc: 3\n}\n', id='the comment lines above a removed member stay with the run before them'
		),
	],
)
def test_remove_layout(text: str, path: tree.Path, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove(path)
	assert tree.unparse(document) == expected


def test_removing_the_first_members_after_a_header_leaves_no_new_blank_line() -> None:
	document = tree.parse('# h\n\na: 1\n\n# c\nb: 2\n\nc: 3\n')
	del document['a']
	del document['b']
	assert tree.unparse(document) == '# h\n\n# c\n\nc: 3\n'


def test_new_items_on_one_line_copy_the_comma_of_the_item_before() -> None:
	document = tree.parse('a: [1, 2,]')
	document.set(('a', 2), 3)
	document.set(('a', 3), 4)
	assert tree.unparse(document) == 'a: [1, 2, 3, 4,]'


# The "Editing" rules of the spec, with the results of soml-js `edit()`.
@pytest.mark.parametrize(
	('text', 'path', 'value', 'expected'),
	[
		pytest.param('a: {b: 1}', ('a', 'c'), 2, 'a: {b: 1, c: 2}', id='a new member on the line of a one-line object'),
		pytest.param('a: [1, 2,]', ('a', 2), 3, 'a: [1, 2, 3,]', id='a new item copies the comma of the item before'),
		pytest.param('a: [\n\t1,\n]', ('a', 1), 2, 'a: [\n\t1,\n\t2\n]', id='a new item on its own line has no comma'),
		pytest.param('a: [\n\t1 # c\n]', ('a', 1), 2, 'a: [\n\t1 # c\n\t2\n]', id='a comment after the item before does not keep the new one on its line'),
		pytest.param('a: {b: 1 /* c */}', ('a', 'd'), 2, 'a: {b: 1 /* c */, d: 2}', id='a new member goes after the comment that the member before owns'),
		pytest.param('a: [1, /* c */ 2]', ('a', 1), None, 'a: [1]', id='the last item takes the comment it owns and the comma before it'),
		pytest.param('a: [1 /* c */, 2]', ('a', 0), None, 'a: [2]', id='an item takes the comment before its comma'),
		pytest.param('a: [/* c */ 1, 2]', ('a', 0), None, 'a: [2]', id='an item takes the comment before it'),
		pytest.param('a: [1, 2 /* c */]', ('a', 1), None, 'a: [1]', id='the last item takes the comment after it'),
		pytest.param('a: [1, /* c */]', ('a', 0), None, 'a: [/* c */]', id="a comment after a comma that the closing bracket follows is not the item's"),
		pytest.param('a: [1, /* c */]', ('a', 1), 2, 'a: [1, 2, /* c */]', id='a new item goes after the comma of the item before'),
		pytest.param('a: [1, 2]', ('a', 1), None, 'a: [1]', id='the last item takes the comma before it'),
		pytest.param('a: [1 , 2]', ('a', 1), None, 'a: [1]', id='the last item takes the comma before it with spaces around'),
		pytest.param('a: [\n\t1, 2\n]', ('a', 1), None, 'a: [\n\t1\n]', id='the last item takes the comma before it and keeps the line break'),
		pytest.param('a: [\n\t1,\n\t2\n]', ('a', 1), None, 'a: [\n\t1,\n]', id='a comma before a line break stays'),
		pytest.param('a: [\n\t1\n\t2,\n]', ('a', 1), None, 'a: [\n\t1\n]', id='no comma is added to the item before'),
		pytest.param('a: [\n\t1,\n\t2,\n\t3\n]', ('a', 2), None, 'a: [\n\t1,\n\t2,\n]', id='a trailing comma is left'),
		pytest.param('a: [1, 2, 3,]', ('a', 2), None, 'a: [1, 2,]', id='a removed item takes its own comma'),
		pytest.param('a: {b: 1,c: 2,d: 3}', ('a', 'c'), None, 'a: {b: 1,d: 3}', id='the spacing of the comma that stays is kept'),
		pytest.param('a: 1\n\nb: 2\n\nc: 3\n', 'b', None, 'a: 1\n\nc: 3\n', id='no two blank lines are left'),
		pytest.param('a: 1\n\nb: 2\n', 'b', None, 'a: 1\n', id='no blank line is left at the end'),
		pytest.param('a: 1\n\nb: 2\n\nc: 3\n\nd: 4\n', ('b',), None, 'a: 1\n\nc: 3\n\nd: 4\n', id='one blank line between the others stays'),
		pytest.param('a: {\n\tb: 1\n\n\tc: 2\n}\n', ('a', 'b'), None, 'a: {\n\tc: 2\n}\n', id='no blank line is left after an opening bracket'),
		pytest.param('a: [\n\t1\n\n\t2\n]\n', ('a', 1), None, 'a: [\n\t1\n]\n', id='no blank line is left before a closing bracket'),
		pytest.param('# Header.\na: 1\n\nb: 2\n', 'a', None, '# Header.\n\nb: 2\n', id='a blank line after a header comment stays'),
		pytest.param('a: 1\n\n# About b.\nb: 2\n', 'a', None, '# About b.\nb: 2\n', id='no blank line is left at the start'),
		pytest.param('# Config\n\nport: 1\n\nhost: 2\n', 'port', None, '# Config\n\nhost: 2\n', id='a blank line after the header counts'),
		pytest.param('a: [\n\t1, 2,\n\t3, 4,\n\t5\n]\n', ('a', 2), None, 'a: [\n\t1, 2,\n\t4,\n\t5\n]\n', id='an item after a removed one on its line keeps the indentation of the line'),
	],
)
def test_editing_rules_of_the_spec(text: str, path: tree.Path, value: soml.Serializable, expected: str) -> None:
	document = tree.parse(text)

	if value is None:
		assert document.remove(path)
	else:
		document.set(path, value)

	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'path', 'value', 'expected'),
	[
		pytest.param('servers: [\n\t# Add servers here.\n]\n', ('servers', 0), 'a', "servers: [\n\t# Add servers here.\n\t'a'\n]\n", id='a new item goes below the comment in an empty array'),
		pytest.param('  a: 1\n\tb: 2\n', 'c', 3, '  a: 1\n\tb: 2\n\tc: 3\n', id='the indentation of the last member'),
		pytest.param('x: {a: 1,\n\tb: 2}', ('x', 'c'), 1, 'x: {a: 1,\n\tb: 2, c: 1}', id='a new member goes on the line of the member before it when the closing brace follows it there'),
	],
)
def test_set_layout(text: str, path: tree.Path, value: int | str, expected: str) -> None:
	document = tree.parse(text)
	document.set(path, value)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('source', 'invalid_path', 'edited_path'),
	[
		('a: true\n', 'a', 'c'),
		('a: true\n', 'a', ('c', 'nested')),
		('[0, true]', 1, 2),
		('[0, true]', 1, (2, 'nested')),
		('a: true\nb: 1\n', 'a', 'b'),
		('[true, 1]', 0, 1),
	],
)
def test_set_undoes_the_change_on_any_error(source: str, invalid_path: tree.Path, edited_path: tree.Path) -> None:
	document = tree.parse(source)
	children = document.body.members if isinstance(document.body, tree.ObjectNode) else document.body.elements
	original_children = tuple(children)
	node = document.get(invalid_path)
	assert isinstance(node, tree.BooleanNode)
	node.value = 1  # type: ignore[assignment]

	with pytest.raises(TypeError, match='A BooleanNode holds a bool'):
		document.set(edited_path, 2)

	assert (document.body.members if isinstance(document.body, tree.ObjectNode) else document.body.elements) is children
	assert len(children) == len(original_children)
	assert all(child is original for child, original in zip(children, original_children, strict=True))
	node.value = True
	assert tree.unparse(document) == source


@pytest.mark.parametrize('path', [b'ab', bytearray(b'ab'), None, 1.5, {'a': 1}, {'a'}, frozenset(['a']), (x for x in ['a'])])
def test_a_path_that_is_bytes_or_not_a_sequence_raises_a_type_error(path: object) -> None:
	document = tree.parse('a: 1\n')

	with pytest.raises(TypeError, match=r'^The path must be a non-empty array of keys and array indexes$'):
		document.get(path)  # type: ignore[arg-type]


def test_a_node_replaced_twice_keeps_the_comments_of_the_original() -> None:
	document = tree.parse('a: [\n\t# lead one\n\t1, # one\n\t2, # two\n]\n')
	document.set(('a', 0), 'x')
	document.set(('a', 0), 'y')
	assert tree.unparse(document) == "a: [\n\t# lead one\n\t'y', # one\n\t2, # two\n]\n"


@pytest.mark.parametrize(
	('text', 'removed', 'path', 'value', 'expected'),
	[
		pytest.param('[1, 2]', 1, 1, 3, '[1, 3]', id='an array on one line'),
		pytest.param('{a: 1, b: 2}', 'b', 'c', 3, '{a: 1, c: 3}', id='an object on one line'),
		pytest.param('x: {a: 1, b: 2}', ('x', 'b'), ('x', 'c'), 3, 'x: {a: 1, c: 3}', id='a nested object on one line'),
		pytest.param('[1 , 2]', 1, 1, 3, '[1 , 3]', id='spaces around the comma'),
		pytest.param('[1 /* c */, 2]', 1, 1, 3, '[1 /* c */, 3]', id='a comment before the comma stays there'),
		pytest.param('[1, 2 /* c */]', 1, 1, 3, '[1, 3]', id='a comment after the removed item goes with it'),
		pytest.param('[1, 2, 3]', 2, 2, 4, '[1, 2, 4]', id='only the last of several items'),
		pytest.param('[1, 2, 3]', 1, 2, 4, '[1, 3, 4]', id='an item in the middle, which takes its own comma'),
		pytest.param('[1, 2, 3,]', 2, 2, 4, '[1, 2, 4,]', id='a trailing comma, which the removed item takes'),
		pytest.param('[1, /* c */ 2]', 1, 1, 3, '[1, 3]', id='a comment between the comma and the removed item goes with it, and so does the comma'),
		pytest.param('[\n\t1,\n\t2\n]', 1, 1, 3, '[\n\t1,\n\t3\n]', id='a comma before a line break stays'),
	],
)
def test_a_new_child_after_removed_last_children_does_not_get_the_comma_they_took(text: str, removed: tree.Path, path: tree.Path, value: int, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove(removed)
	document.set(path, value)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'paths', 'expected'),
	[
		pytest.param('[1, 2,\n# end\n]\n', [1, 0], '[\n# end\n]\n', id='every item on the line of the bracket'),
		pytest.param('[1,\n# about 2\n2\n]\n', [1, 0], '[\n# about 2\n]\n', id='the comment line above a removed item stays'),
		pytest.param('{a: 1, b: 2,\n\t# end\n}', ['a', 'b'], '{\n\t# end\n}', id='every member on the line of the brace'),
	],
)
def test_removing_every_child_on_the_line_of_the_opening_bracket_keeps_the_comment_lines_after_them_on_their_own_lines(text: str, paths: list[tree.Path], expected: str) -> None:
	document = tree.parse(text)

	for path in paths:
		assert document.remove(path)

	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'removed', 'path', 'value', 'expected'),
	[
		pytest.param('{a: 1}', 'a', 'b', 2, '{\n\tb: 2\n}', id='a braced document on one line'),
		pytest.param('x: {a: 1}\n', ('x', 'a'), ('x', 'b'), 2, 'x: {\n\tb: 2\n}\n', id='a nested object on one line'),
		pytest.param('x: [1]\n', ('x', 0), ('x', 0), 2, 'x: [\n\t2\n]\n', id='an array on one line'),
		pytest.param('x: [1, [5]]\n', ('x', 1, 0), ('x', 1, 0), 9, 'x: [1, [9]]\n', id='an array inside an array on one line stays on that line'),
		pytest.param('\tx: {a: 1, b: 2}\n', ('x', 'a'), ('x', 'c'), 3, '\tx: {b: 2, c: 3}\n', id='a child that stays keeps the line'),
		pytest.param('x: { /* c */ a: 1 }\n', ('x', 'a'), ('x', 'b'), 2, 'x: {\n\tb: 2\n}\n', id='a comment in front of the removed member goes with it'),
		pytest.param('x: {a: 1 /* c */}\n', ('x', 'a'), ('x', 'b'), 2, 'x: {\n\tb: 2\n}\n', id='a comment after the removed member goes with it'),
		pytest.param('{\n\tx: {a: 1}\n}\n', ('x', 'a'), ('x', 'b'), 2, '{\n\tx: {\n\t\tb: 2\n\t}\n}\n', id='the indentation of the line of the container'),
		pytest.param('x: {a: 1\n\n}\n', ('x', 'a'), ('x', 'b'), 2, 'x: {\n\tb: 2\n}\n', id='a closing bracket on its own line after a blank line'),
		pytest.param('x: {a: 1\n}\n', ('x', 'a'), ('x', 'b'), 2, 'x: {\n\tb: 2\n}\n', id='a closing bracket on its own line'),
		pytest.param('x: [1 # c\n\n]\n', ('x', 0), ('x', 0), 2, 'x: [\n\t2\n]\n', id='a comment at the end of the line of the removed item'),
		pytest.param('x: [1\n\t/* c */]\n', ('x', 0), ('x', 0), 2, 'x: [\n\t2\n\t/* c */]\n', id='a comment that begins the line of the closing bracket stays there'),
		pytest.param('x: [1 /* c */\n]\n', ('x', 0), ('x', 0), 2, 'x: [\n\t2\n]\n', id='a comment at the end of the line of the removed item goes with it'),
		pytest.param('{\n\tx: [1\n\n\t]\n}\n', ('x', 0), ('x', 0), 2, '{\n\tx: [\n\t\t2\n\t]\n}\n', id='the indentation of the closing bracket stays'),
	],
)
def test_a_new_child_in_a_container_whose_children_were_all_removed_goes_on_its_own_line(text: str, removed: tree.Path, path: tree.Path, value: soml.Serializable, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove(removed)
	document.set(path, value)
	assert tree.unparse(document) == expected

	# The same as two separate edits.
	document = tree.parse(text)
	document.remove(removed)
	document = tree.parse(tree.unparse(document))
	document.set(path, value)
	assert tree.unparse(document) == expected


@pytest.mark.parametrize(
	('text', 'removed', 'path', 'value', 'expected'),
	[
		pytest.param('[\n\t1, 2,\n\t3, 4\n]\n', 3, 3, 5, '[\n\t1, 2,\n\t3, 5\n]\n', id='an array'),
		pytest.param('a: [\n\t1, 2\n]\n', ('a', 1), ('a', 1), 3, 'a: [\n\t1, 3\n]\n', id='a nested array'),
		pytest.param('{\n\ta: 1, b: 2\n}\n', 'b', 'c', 3, '{\n\ta: 1, c: 3\n}\n', id='an object'),
		pytest.param('x: {\n\ty: [\n\t\t1, 2\n\t]\n}\n', ('x', 'y', 1), ('x', 'y', 1), 3, 'x: {\n\ty: [\n\t\t1, 3\n\t]\n}\n', id='the indentation of the bracket stays'),
		pytest.param('a: [\n\t1, 2 # two\n]\n', ('a', 1), ('a', 1), 3, 'a: [\n\t1, 3\n]\n', id='the comment at the end of the removed line goes'),
		pytest.param('a: [\n\t1, 2]\n', ('a', 1), ('a', 1), 3, 'a: [\n\t1, 3]\n', id='a bracket on the line of the last item stays there'),
	],
)
def test_a_new_last_child_keeps_the_closing_bracket_on_its_own_line(text: str, removed: tree.Path, path: tree.Path, value: int, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove(removed)
	document.set(path, value)
	assert tree.unparse(document) == expected


def test_a_new_item_in_an_array_that_holds_only_a_comment_owns_that_comment() -> None:
	document = tree.parse('[/* c */]')
	document.set(0, 1)
	text = tree.unparse(document)
	assert text == '[/* c */ 1]'

	# The comment is before the item on its line, so removing the item from that text takes the comment too, as in soml-js.
	document = tree.parse(text)
	assert document.remove(0)
	assert tree.unparse(document) == '[]'

	# In the same tree, the item was never in the source, so the source stays as it was.
	document = tree.parse('[/* c */]')
	document.set(0, 1)
	assert document.remove(0)
	assert tree.unparse(document) == '[/* c */]'


def test_a_new_last_member_ends_its_line_before_comment_lines_after_it() -> None:
	document = tree.parse('a: 1\n# About b.\nb: 2')
	document.remove('b')
	document.set('c', 3)
	assert tree.unparse(document) == 'a: 1\nc: 3\n# About b.\n'


@pytest.mark.parametrize(
	('text', 'edit', 'expected'),
	[
		pytest.param('ports: [80 /* http */]', lambda document: document.set(('ports', 1), 443), 'ports: [80 /* http */, 443]', id='a new item goes after the comment before a closing bracket'),
		pytest.param('p: [80, 443 /* https */]', lambda document: document.remove(('p', 1)), 'p: [80]', id='a removed item takes the comment before the closing bracket'),
	],
)
def test_a_comment_before_a_closing_bracket_belongs_to_the_child_before_it(text: str, edit: Callable[[tree.DocumentNode], object], expected: str) -> None:
	document = tree.parse(text)
	edit(document)
	assert tree.unparse(document) == expected


def test_printing_many_replaced_items_takes_linear_time() -> None:
	def measure(count: int) -> float:
		document = tree.parse('a: [' + ', '.join(map(str, range(count))) + ']\n')
		array = document.get('a')
		assert isinstance(array, tree.ArrayNode)

		# What `set()` records for each item, without reading the document back after each change, which `set()` does on purpose.
		for index, element in enumerate(array.elements):
			node = tree.from_value(-index)
			node._replaces = element
			array.elements[index] = node

		return best_time(lambda: tree.unparse(document))

	measure(100)
	assert measure(16_000) / measure(2000) < 24


def test_removing_every_item_keeps_a_comment_line_in_a_braced_container() -> None:
	document = tree.parse('a: [\n\t# head\n\t1,\n]\n')
	assert document.remove(('a', 0))
	assert tree.unparse(document) == 'a: [\n\t# head\n]\n'


def test_removing_an_index_under_an_object_is_refused() -> None:
	with pytest.raises(ValueError, match=re.escape('Cannot edit postgres[0], because postgres is an object, so it needs a key, not an index')):
		tree.parse('postgres: {port: 1}\n').remove(('postgres', 0))


def test_a_path_in_an_error_is_sanitized() -> None:
	document = tree.parse('a: 1\n')

	with pytest.raises(ValueError, match=re.escape('Cannot edit a."\\u{202e}", because a is not an object or an array')):
		document.set(('a', '\u202e'), 1)


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		pytest.param('  a: 1\n', '  b: 1\n', id='spaces'),
		pytest.param('\ta: 1\n', '\tb: 1\n', id='a tab'),
		pytest.param('# h\n  a: 1\n', '# h\n  b: 1\n', id='after a comment'),
		pytest.param('\n\n  a: 1\n', '\n\n  b: 1\n', id='after blank lines'),
	],
)
def test_a_new_member_in_a_brace_less_body_whose_members_were_removed_keeps_the_indentation_once(text: str, expected: str) -> None:
	document = tree.parse(text)
	assert document.remove('a')
	document.set('b', 1)
	assert tree.unparse(document) == expected


def test_a_new_nested_member_in_an_indented_brace_less_body_lines_up_with_its_key() -> None:
	document = tree.parse('# h\n  a: 1\n')
	assert document.remove('a')
	document.set('b', {'c': [1]})
	assert tree.unparse(document) == '# h\n  b: {\n  \tc: [\n  \t\t1\n  \t]\n  }\n'


def test_a_second_new_member_after_the_first_in_an_emptied_indented_body_lines_up_with_it() -> None:
	document = tree.parse('\ta: 1\n\tb: 2\n')
	assert document.remove('a')
	assert document.remove('b')
	document.set('c', 1)
	document.set('d', 1)
	assert tree.unparse(document) == '\tc: 1\n\td: 1\n'


def test_a_member_inserted_first_in_an_indented_brace_less_body_does_not_move_the_old_first_member() -> None:
	document = tree.parse('# h\n  a: 1\n  b: 2\n')
	new_body = tree.DocumentNode.from_value({'z': 1}).body
	assert isinstance(document.body, tree.ObjectNode)
	assert isinstance(new_body, tree.ObjectNode)
	member = new_body.members[0]
	member.comments = ['c']
	document.body.members.insert(0, member)
	assert tree.unparse(document) == '# h\n  # c\n  z: 1\n  a: 1\n  b: 2\n'


def test_removing_every_member_of_a_brace_less_body_does_not_leave_a_run_of_blank_lines() -> None:
	document = tree.parse('a: 1\n# b\n\nb: 2\n\n# end\n')
	assert document.remove('a')
	assert document.remove('b')
	assert tree.unparse(document) == '{}\n# b\n\n# end\n'


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		pytest.param('[ /* a\n b */ ]\n', 0, '[ /* a\n b */\n\t1\n]\n', id='array'),
		pytest.param('x: { /* a\n b */ }\n', ('x', 'a'), 'x: { /* a\n b */\n\ta: 1\n}\n', id='object'),
	],
)
def test_a_new_child_in_an_empty_container_with_a_multi_line_block_comment_goes_on_its_own_line(text: str, path: str | int | tuple[str, ...], expected: str) -> None:
	document = tree.parse(text)
	document.set(path, 1)
	assert tree.unparse(document) == expected


def test_a_new_member_after_the_members_of_an_indented_brace_less_body_lines_up_with_them() -> None:
	document = tree.parse('  a: 1\n  b: 2\n')
	document.set('c', 3)
	assert tree.unparse(document) == '  a: 1\n  b: 2\n  c: 3\n'


def test_replacing_a_member_of_an_indented_brace_less_body_keeps_the_indentation() -> None:
	document = tree.parse('# h\n\t\ta: 1\n\t\tb: 2\n')
	document.set('a', 5)
	assert tree.unparse(document) == '# h\n\t\ta: 5\n\t\tb: 2\n'


def test_removing_the_first_member_of_an_indented_brace_less_body_keeps_the_indentation_of_the_next() -> None:
	document = tree.parse('  a: 1\n  b: 2\n')
	assert document.remove('a')
	assert tree.unparse(document) == '  b: 2\n'


def test_a_brace_less_body_that_was_emptied_and_filled_again_reads_back_as_what_was_set() -> None:
	document = tree.parse('# h\n  a: 1\n')
	assert document.remove('a')
	document.set('b', {'c': [1, 2]})
	document.set('d', 'x')
	assert soml.loads(tree.unparse(document)) == {'b': {'c': [1, 2]}, 'd': 'x'}


def test_removing_every_member_of_a_brace_less_body_keeps_the_comments_and_reads_back_as_an_empty_object() -> None:
	document = tree.parse('# one\na: 1\n# two\nb: 2 # three\n\n# end\n')
	assert document.remove('a')
	assert document.remove('b')
	text = tree.unparse(document)
	assert soml.loads(text) == {}
	assert '# one' in text
	assert '# two' in text
	assert '# end' in text
	assert '# three' not in text


def test_a_second_new_child_in_an_empty_container_with_a_multi_line_block_comment_follows_the_first() -> None:
	document = tree.parse('x: [ /* a\n b */ ]\n')
	document.set(('x', 0), 1)
	document.set(('x', 1), 2)
	assert tree.unparse(document) == 'x: [ /* a\n b */\n\t1\n\t2\n]\n'


@pytest.mark.parametrize(
	('text', 'path', 'expected'),
	[
		pytest.param('x: {\n\ty: [ /* a\n\t b */ ]\n}\n', ('x', 'y', 0), 'x: {\n\ty: [ /* a\n\t b */\n\t\t1\n\t]\n}\n', id='an array in an object'),
		pytest.param('  x: { /* a\n b */ }\n', ('x', 'a'), '  x: { /* a\n b */\n  \ta: 1\n  }\n', id='an object in an indented body'),
	],
)
def test_the_closing_bracket_keeps_its_indentation_after_a_new_child_in_an_empty_container_with_a_multi_line_block_comment(text: str, path: tuple[str | int, ...], expected: str) -> None:
	document = tree.parse(text)
	document.set(path, 1)
	assert tree.unparse(document) == expected


DOCUMENTS_FOR_SEQUENCES = [
	'a: 1\nb: {c: 2}\n',
	'list: [1, 2, [3]]\n',
	"ports: [80, 443]\nname: 'x'\n",
	'x: {}\n',
	'y: []\n',
	'deep: {a: {b: [1, {c: 2}]}}\n',
	'{oneline: 1, keep: 2}\n',
	'arr: [1, 2]\n',
	'top: [{a: 1}, {b: [2]}]\n',
	'e: [/* c */]\n',
	'# Note\nkeep: true\nstats: [1, 2, 3]\n',
]

VALUES_FOR_SEQUENCES: list[soml.Serializable] = [1, -2, 0.5, 'text', 'multi\nline', True, False, None, [], [1, 2], {}, {'k': 1}, {'k': [1, {'m': 2}]}, timedelta(seconds=90), 2**62]

KEYS_FOR_SEQUENCES = ['new', 'k', 'a.b', 'with space', '2024-01-01', 'c', 'list', 'top', 'x']


def a_random_path(value: soml.Value, rng: random.Random, prefix: tuple[str | int, ...] = ()) -> tuple[str | int, ...] | None:
	"""A path into a plain value, or into a fresh append position, or `None` for a scalar at the top."""
	if prefix and rng.random() < 0.2:
		return prefix

	if isinstance(value, dict):
		if not value or rng.random() < 0.25:
			return (*prefix, rng.choice(KEYS_FOR_SEQUENCES))

		key = rng.choice([*value])
		return a_random_path(value[key], rng, (*prefix, key))

	if isinstance(value, list):
		if not value or rng.random() < 0.25:
			return (*prefix, len(value))

		index = rng.randrange(len(value))
		return a_random_path(value[index], rng, (*prefix, index))

	return prefix or None


def found_at(value: soml.Value, path: tuple[str | int, ...]) -> object:
	"""The value at a path in a plain value, or a sentinel when nothing is there."""
	for segment in path[:-1]:
		if isinstance(value, dict) and isinstance(segment, str):
			if segment not in value:
				return MISSING

			value = value[segment]
		elif isinstance(value, list) and isinstance(segment, int) and segment < len(value):
			value = value[segment]
		else:
			return MISSING

	last = path[-1]

	if isinstance(value, dict) and isinstance(last, str):
		return value.get(last, MISSING)

	if isinstance(value, list) and isinstance(last, int) and last < len(value):
		return value[last]

	return MISSING


MISSING = object()


def is_there(value: soml.Value, path: tuple[str | int, ...]) -> bool:
	return found_at(value, path) is not MISSING


@pytest.mark.parametrize('seed', range(8))
def test_a_sequence_of_edits_keeps_the_tree_in_agreement_with_its_text(seed: int) -> None:
	"""
	Every edit of a random sequence leaves a valid document that is a fixed point, whose text reads back as the value the tree itself holds. A successful `set()` puts its value at the path, a successful `remove()` takes the value away, and a failed one changes nothing.
	"""
	rng = random.Random(seed)

	for iteration in range(400):
		document = tree.parse(DOCUMENTS_FOR_SEQUENCES[iteration % len(DOCUMENTS_FOR_SEQUENCES)])
		output = tree.unparse(document)

		for _ in range(rng.randint(1, 6)):
			value = soml.loads(output)
			path = a_random_path(value, rng)

			if path is None:
				continue

			if rng.random() < 0.65:
				new_value = rng.choice(VALUES_FOR_SEQUENCES)

				try:
					document.set(path, new_value)
				except TypeError, ValueError:
					# Nothing changes after an error.
					assert tree.unparse(document) == output
					continue

				output = tree.unparse(document)
				assert typed(found_at(soml.loads(output), path)) == typed(new_value)
			else:
				if remove_raises(value, path):
					# A path that goes through a scalar, or that uses the wrong kind of segment, raises, and changes nothing.
					with pytest.raises(ValueError, match='Cannot edit'):
						document.remove(path)

					assert tree.unparse(document) == output
					continue

				assert document.remove(path) == is_there(value, path)
				output = tree.unparse(document)

			read_back = tree.parse(output)
			assert typed(read_back.to_value()) == typed(document.to_value())
			assert tree.unparse(read_back) == output


def remove_raises(value: soml.Value, path: tuple[str | int, ...]) -> bool:
	"""Whether `remove()` raises for the path: it goes through a scalar, or uses a key where an array is, or an index where an object is. A path that merely leads to nothing does not raise."""
	for segment in path[:-1]:
		if isinstance(value, dict) and isinstance(segment, str):
			if segment not in value:
				return False

			value = value[segment]
		elif isinstance(value, list) and isinstance(segment, int):
			if segment >= len(value):
				return False

			value = value[segment]
		else:
			return True

	if isinstance(value, dict):
		return not isinstance(path[-1], str)

	if isinstance(value, list):
		return not isinstance(path[-1], int)

	return True
