"""
Unit tests for `loads()` and `load()`, ported from soml-js's `test/parse.ts` and from the Swift port's tests, for what the conformance suite does not cover.

The JS tests that do not apply to Python are left out: the `integers` option, prototype pollution, realms, `Temporal`, and code frames. The nesting limit is 100 here, where it is 1000 in JS, and an instant or a duration finer than a microsecond is an error, because `datetime` and `timedelta` cannot hold it.
"""

import contextlib
import enum
import io
import math
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import EDGE_SERVICE_DOCUMENT, EVERY_FEATURE_DOCUMENT, decode_error, load_object, load_value, typed

import soml

MAX_DEPTH = 100

SUB_MICROSECOND_MESSAGE = r"finer than a microsecond, which Python's (datetime|timedelta) cannot hold"


# Input


def test_accepts_a_string() -> None:
	assert typed(soml.loads('a: 1')) == typed({'a': 1})


def test_accepts_utf8_bytes() -> None:
	assert soml.loads("a: 'é😀'".encode()) == {'a': 'é😀'}


def test_accepts_a_bytearray() -> None:
	assert typed(soml.loads(bytearray(b'a: 1'))) == typed({'a': 1})


@pytest.mark.parametrize('base', [bytes, bytearray])
def test_bytes_are_decoded_strictly_even_when_a_subclass_overrides_decode(base: type[bytes | bytearray]) -> None:
	class Overriding(base):  # type: ignore[valid-type, misc]
		def decode(self, *arguments: Any, **keywords: Any) -> str:  # noqa: ARG002
			return "x: 'replaced'"

		def __str__(self) -> str:
			raise AssertionError('Decoding must not use the string representation')

		def __bytes__(self) -> bytes:
			raise AssertionError('Decoding must not convert the buffer through __bytes__')

	assert typed(soml.loads(Overriding(b'a: 1'))) == typed({'a': 1})
	assert soml.format(Overriding(b'a:   1')) == 'a: 1\n'
	assert soml.tree.parse(Overriding("a: 'é😀'".encode())).to_value() == {'a': 'é😀'}

	with pytest.raises(soml.SOMLDecodeError, match='Invalid UTF-8 byte 0xFF'):
		soml.loads(Overriding(b"a: '\xff'"))


def test_reads_a_str_subclass_by_its_content() -> None:
	# An enum that mixes in `str` formats as `Document.SOURCE`, not as its content. A `StrEnum` formats as its content, so it does not show this.
	class Document(str, enum.Enum):  # noqa: UP042
		SOURCE = 'port: 8080'

	assert typed(soml.loads(Document.SOURCE)) == typed({'port': 8080})


def test_empty_bytes_are_an_empty_invalid_document() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='empty'):
		soml.loads(b'')


@pytest.mark.parametrize('value', [None, 1, 1.0, True, {}, [], memoryview(b'a: 1'), Path('a.soml')])
def test_rejects_other_input_types_with_a_type_error(value: Any) -> None:
	with pytest.raises(TypeError, match=f'not {type(value).__name__!r}'):
		soml.loads(value)


def test_a_byte_order_mark_in_bytes_is_rejected_rather_than_stripped() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='byte order mark'):
		soml.loads(b'\xef\xbb\xbfa: 1')


def test_invalid_utf8_is_reported_with_the_line_and_column_of_the_bad_byte() -> None:
	error = decode_error("a: 1\nb: 'é".encode() + b"\xff'")
	assert (error.msg, error.lineno, error.colno) == ('Invalid UTF-8 byte 0xFF', 2, 6)


def test_invalid_utf8_after_a_byte_order_mark_counts_the_mark_as_a_column() -> None:
	error = decode_error(b'\xef\xbb\xbfa\xff')
	assert (error.msg, error.lineno, error.colno) == ('Invalid UTF-8 byte 0xFF', 1, 3)


def test_invalid_utf8_at_the_very_start() -> None:
	error = decode_error(b'\x80')
	assert (error.msg, error.lineno, error.colno) == ('Invalid UTF-8 byte 0x80', 1, 1)


def test_a_utf8_sequence_cut_short_by_the_end_of_the_input() -> None:
	error = decode_error(b'a: 1 #\xe2\x82')
	assert (error.msg, error.lineno, error.colno) == ('Incomplete UTF-8 sequence at the end of the input', 1, 7)


def test_a_cut_utf8_sequence_that_is_not_at_the_end_is_invalid_not_incomplete() -> None:
	assert decode_error(b"a: '\xe2\x82'").msg == 'Invalid UTF-8 byte 0xE2'


@pytest.mark.parametrize(
	'invalid',
	[
		pytest.param(b'\x80', id='a lone continuation byte'),
		pytest.param(b'\xc3', id='a truncated two-byte sequence'),
		pytest.param(b'\xc0\xaf', id='an overlong encoding of a slash'),
		pytest.param(b'\xc0\x80', id='an overlong encoding of NUL'),
		pytest.param(b'\xed\xa0\x80', id='an encoded high surrogate'),
		pytest.param(b'\xed\xb0\x80', id='an encoded low surrogate'),
		pytest.param(b'\xf4\x90\x80\x80', id='a code point above U+10FFFF'),
		pytest.param(b'\xff', id='an invalid byte'),
		pytest.param(b'\xf0\x9f\x98', id='a truncated four-byte sequence'),
	],
)
def test_invalid_utf8_is_rejected_with_its_position(invalid: bytes) -> None:
	error = decode_error(b"a: 1\nb: '" + invalid + b"'")
	assert error.msg.startswith('Invalid UTF-8 byte')
	assert (error.lineno, error.colno) == (2, 5)


def test_invalid_utf8_in_a_comment_is_rejected_too() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Invalid UTF-8'):
		soml.loads(b'# \xff\na: 1')


@pytest.mark.parametrize(
	('source', 'reason', 'column'),
	[
		("a: '\ud800'", 'A lone surrogate (U+D800) is not a Unicode scalar value', 5),
		("a: '\udc00'", 'A lone surrogate (U+DC00) is not a Unicode scalar value', 5),
		("a: 'x\ud83d'", 'A lone surrogate (U+D83D) is not a Unicode scalar value', 6),
		('# \ud800\na: 1', 'A lone surrogate (U+D800) is not a Unicode scalar value', 3),
	],
)
def test_a_lone_surrogate_in_a_string_input_is_rejected(source: str, reason: str, column: int) -> None:
	error = decode_error(source)
	assert (error.msg, error.colno) == (reason, column)


def test_a_lone_surrogate_from_surrogateescape_is_rejected() -> None:
	text = b"a: '\xff'".decode(errors='surrogateescape')

	with pytest.raises(soml.SOMLDecodeError, match='lone surrogate'):
		soml.loads(text)


def test_an_astral_character_is_accepted() -> None:
	assert soml.loads("a: '\U0001f600'") == {'a': '😀'}


def test_load_reads_a_binary_file() -> None:
	assert typed(soml.load(io.BytesIO(b'a: 1'))) == typed({'a': 1})


def test_load_reads_a_file_from_disk(tmp_path: Path) -> None:
	path = tmp_path / 'config.soml'
	path.write_bytes("name: 'é'\nport: 8080\n".encode())

	with path.open('rb') as file:
		assert typed(soml.load(file)) == typed({'name': 'é', 'port': 8080})


def test_load_rejects_a_text_file() -> None:
	file: Any = io.StringIO('a: 1')

	with pytest.raises(TypeError, match=r"open\('config.soml', 'rb'\)"):
		soml.load(file)


@pytest.mark.parametrize(
	('content', 'encoding'),
	[
		pytest.param(b"name: '\xff'\n", 'utf-8', id='invalid UTF-8'),
		pytest.param("name: 'å'\n".encode(), 'ascii', id='a character that the encoding does not have'),
	],
)
def test_load_rejects_a_text_file_that_cannot_decode_its_content_before_reading_it(tmp_path: Path, content: bytes, encoding: str) -> None:
	path = tmp_path / 'config.soml'
	path.write_bytes(content)

	with path.open(encoding=encoding) as file, pytest.raises(TypeError, match=r"open\('config.soml', 'rb'\)"):
		soml.load(file)  # type: ignore[arg-type]


def test_load_raises_a_decode_error_for_an_invalid_document() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Duplicate key a'):
		soml.load(io.BytesIO(b'a: 1\na: 2'))


# Type mapping


def test_a_document_with_every_type_parses_to_the_matching_python_types() -> None:
	value = load_object("""
string: 'x'
int: 1
float: 1.0
true: true
false: false
null: null
instant: 2026-09-19T14:00:00Z
duration: 90m
array: []
object: {}
""")
	assert typed(value) == typed(
		{
			'string': 'x',
			'int': 1,
			'float': 1.0,
			'true': True,
			'false': False,
			'null': None,
			'instant': datetime(2026, 9, 19, 14, tzinfo=UTC),
			'duration': timedelta(minutes=90),
			'array': [],
			'object': {},
		}
	)


def test_containers_are_plain_dicts_and_lists() -> None:
	value = load_object('a: {b: [{c: 1}]}\nd: {e: {f: 1}}')
	assert type(value) is dict
	assert type(value['a']) is dict
	assert typed(value['a']) == typed({'b': [{'c': 1}]})
	assert type(value['a']['b']) is list
	assert type(value['d']) is dict
	assert type(soml.loads('[1]')) is list


def test_an_int_and_a_float_are_different_types() -> None:
	value = load_object('a: 1\nb: 1.0')
	assert type(value['a']) is int
	assert type(value['b']) is float


@pytest.mark.parametrize('source', ['-0.0', '-0e5', '-0e0', '-0.0e-5', '-0.000'])
def test_negative_zero_becomes_zero(source: str) -> None:
	value = load_value(source)
	assert isinstance(value, float)
	assert value == 0
	assert math.copysign(1, value) == 1


def test_infinity_parses_to_a_float_infinity() -> None:
	assert typed(soml.loads('a: infinity\nb: -infinity\nc: [infinity, -infinity]')) == typed({'a': math.inf, 'b': -math.inf, 'c': [math.inf, -math.inf]})


def test_an_instant_is_an_aware_datetime_in_utc() -> None:
	value = load_value('2026-09-19T16:00:00+02:00')
	assert isinstance(value, datetime)
	assert value.tzinfo is UTC
	assert value == datetime(2026, 9, 19, 14, tzinfo=UTC)


def test_instants_keep_microseconds() -> None:
	assert load_value('2026-09-19T14:00:00.123456Z') == datetime(2026, 9, 19, 14, 0, 0, 123_456, tzinfo=UTC)


def test_an_instant_fraction_with_trailing_zeros_below_a_microsecond_is_accepted() -> None:
	assert load_value('2026-09-19T14:00:00.123456000Z') == datetime(2026, 9, 19, 14, 0, 0, 123_456, tzinfo=UTC)
	assert load_value('2026-09-19T14:00:00.100000000Z') == datetime(2026, 9, 19, 14, 0, 0, 100_000, tzinfo=UTC)


def test_instants_are_normalized_to_the_same_instant_regardless_of_offset() -> None:
	value = load_object('a: 2026-09-19T14:00:00Z\nb: 2026-09-19T16:00:00+02:00\nc: 2026-09-19T14:00:00.000Z\nd: 2026-09-19T14:00:00+00:00')
	assert value['a'] == value['b'] == value['c'] == value['d']


def test_a_duration_is_a_timedelta() -> None:
	value = load_value('90m')
	assert isinstance(value, timedelta)
	assert value == timedelta(hours=1, minutes=30)


def test_an_int_is_exact_across_the_whole_int64_range() -> None:
	assert load_value('9223372036854775807') == 2**63 - 1
	assert load_value('-9223372036854775808') == -(2**63)
	assert load_value('9007199254740993') == 9_007_199_254_740_993


@pytest.mark.parametrize(
	'source',
	[
		'2026-09-19T14:00:00.123456789Z',
		'2026-09-19T14:00:00.0000001Z',
		'2026-09-19T14:00:00.000000001Z',
		'2026-09-19T14:00:00.000000100Z',
		'1969-12-31T23:59:59.999999999Z',
		'9999-12-31T23:59:59.999999999Z',
		'2026-09-19T16:00:00.1234567+02:00',
	],
)
def test_an_instant_finer_than_a_microsecond_is_rejected(source: str) -> None:
	error = decode_error(f'a: {source}')
	assert error.msg == f"Invalid instant {source}: it is finer than a microsecond, which Python's datetime cannot hold"
	assert (error.lineno, error.colno) == (1, 4)


@pytest.mark.parametrize(
	'source',
	[
		'1ns',
		'7ns',
		'999ns',
		'1001ns',
		'1.000ns',
		'1.5us',
		'0.000000001s',
		'1h0.000000001s',
		'1_000.000_5ms',
		'0.00000000005m',
		'0.0000000000025h',
		'59m59.999999999s',
		'1h2m3s4ms5us6ns',
		'9223372036854775807ns',
		'2562047h47m16s854ms775us807ns',
		'2562047h47m16.854775807s',
		'-9223372036854775808ns',
		'-2562047h47m16s854ms775us808ns',
		'-2562047h47m16.854775808s',
		'-1ns',
	],
)
def test_a_duration_finer_than_a_microsecond_is_rejected(source: str) -> None:
	error = decode_error(f'a: {source}')
	assert error.msg == f"Invalid duration {source}: it is finer than a microsecond, which Python's timedelta cannot hold"
	assert (error.lineno, error.colno) == (1, 4)


def test_the_sub_microsecond_error_is_raised_inside_containers_too() -> None:
	with pytest.raises(soml.SOMLDecodeError, match=SUB_MICROSECOND_MESSAGE):
		soml.loads('[1s, {a: [1ns]}]')


# Documents


def test_a_brace_less_object() -> None:
	assert soml.loads("name: 'x'\nversion: '1.0.0'") == {'name': 'x', 'version': '1.0.0'}


def test_a_braced_object_is_the_same_value() -> None:
	assert soml.loads("{name: 'x', version: '1.0.0'}") == soml.loads("name: 'x'\nversion: '1.0.0'")


def test_an_array() -> None:
	assert typed(soml.loads("[1, 'two', true, null]")) == typed([1, 'two', True, None])


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		pytest.param('[\n80\n443\n]', [80, 443], id='items on their own lines'),
		pytest.param('{\na: 1\nb: 2\n}', {'a': 1, 'b': 2}, id='members on their own lines'),
		pytest.param('[1, 2\n3]', [1, 2, 3], id='commas and line breaks mixed'),
		pytest.param('[1,\n]', [1], id='a trailing comma before a line break'),
		pytest.param('[1 # x\n2]', [1, 2], id='a line comment ends at the line break that separates'),
		pytest.param('[1 /* x */\n2]', [1, 2], id='a line break after a block comment separates'),
		pytest.param('{a:\n1}', {'a': 1}, id='a line break after the colon is whitespace'),
		pytest.param("a: {\n\tb: '''\n\t\tx\n\t\t'''\n\tc: 1\n}", {'a': {'b': 'x', 'c': 1}}, id='a line break after a block string separates'),
		pytest.param('[\n\t[1]\n\t{a: 1}\n]', [[1], {'a': 1}], id='containers on their own lines'),
	],
)
def test_a_line_break_separates_items_inside_brackets(source: str, expected: soml.Document) -> None:
	assert typed(soml.loads(source)) == typed(expected)


def test_the_form_is_decided_by_the_first_character_after_comments() -> None:
	assert typed(soml.loads('# x\n/* y */ [1]')) == typed([1])
	assert typed(soml.loads('# x\n/* y */ {a: 1}')) == typed({'a': 1})


def test_the_order_of_members_does_not_change_the_value() -> None:
	assert soml.loads('{a: 1, b: 2}') == soml.loads('{b: 2, a: 1}')


def test_members_keep_their_document_order() -> None:
	assert list(load_object('z: 1\na: 2\nm: {x: 3}\nb: 4')) == ['z', 'a', 'm', 'b']


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		pytest.param('a: 1', {'a': 1}, id='a one-entry bare object'),
		pytest.param('a: 1\n', {'a': 1}, id='a bare object with a trailing line feed'),
		pytest.param('a: 1\n\n\n', {'a': 1}, id='a bare object with many trailing line feeds'),
		pytest.param('\n\n\na: 1', {'a': 1}, id='leading line feeds'),
		pytest.param('   a: 1', {'a': 1}, id='leading spaces'),
		pytest.param('  a: 1\n    b: 2\n\tc: 3', {'a': 1, 'b': 2, 'c': 3}, id='indented entries'),
		pytest.param('a: 1   \nb: 2\t\t', {'a': 1, 'b': 2}, id='trailing spaces'),
		pytest.param('a: 1\n  \n\t\nb: 2', {'a': 1, 'b': 2}, id='whitespace-only lines between entries'),
		pytest.param('{a: 1}', {'a': 1}, id='a braced object'),
		pytest.param('{\n\ta: 1,\n\tb: 2,\n}', {'a': 1, 'b': 2}, id='a braced object across lines'),
		pytest.param('{\n\ta: 1,\n\tb: 2\n}', {'a': 1, 'b': 2}, id='a braced object without a trailing comma'),
		pytest.param('{}', {}, id='an empty object'),
		pytest.param('{ \n\t }', {}, id='an empty object with whitespace'),
		pytest.param("[1, 'two', 3.0]", [1, 'two', 3.0], id='an array'),
		pytest.param('[]', [], id='an empty array'),
		pytest.param('[{a: 1}, {a: 2}]', [{'a': 1}, {'a': 2}], id='an array of objects'),
		pytest.param('[[], [[]], [[[]]]]', [[], [[]], [[[]]]], id='an array of arrays'),
		pytest.param('a:\n\n\t1', {'a': 1}, id='a value on the next line'),
		pytest.param('a: {\n\tb: 1,\n}\nc: 2', {'a': {'b': 1}, 'c': 2}, id='an object value across lines in a bare object'),
		pytest.param('a: [\n\t1,\n\t2,\n]\nb: 3', {'a': [1, 2], 'b': 3}, id='an array value across lines in a bare object'),
		pytest.param('{a: 1, b: [1, 2], c: {d: true}}', {'a': 1, 'b': [1, 2], 'c': {'d': True}}, id='a document that is one line of braces'),
		pytest.param(
			"[1, 'two', true, null, 1.5, 2026-01-01T00:00:00Z, 1s, [], {}]",
			[1, 'two', True, None, 1.5, datetime(2026, 1, 1, tzinfo=UTC), timedelta(seconds=1), [], {}],
			id='mixed types in an array',
		),
		pytest.param('a: true\nb: false\nc: null', {'a': True, 'b': False, 'c': None}, id='keywords as values'),
		pytest.param('[1,2,3]', [1, 2, 3], id='numbers separated by commas without spaces'),
		pytest.param('{a:1,b:2.5}', {'a': 1, 'b': 2.5}, id='members without spaces'),
	],
)
def test_parses_a_document(source: str, expected: soml.Document) -> None:
	assert typed(soml.loads(source)) == typed(expected)


@pytest.mark.parametrize(
	'source',
	[
		'a:1',
		'a:     1',
		'a:\t1',
		'a:\n\t1',
		'a: # comment\n\t1',
		'a: /* comment */ 1',
		'{a:\n1}',
	],
)
def test_whitespace_and_line_breaks_are_allowed_after_the_colon(source: str) -> None:
	assert typed(soml.loads(source)) == typed({'a': 1})


# Comments


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		pytest.param('# comment\na: 1', {'a': 1}, id='a line comment'),
		pytest.param('a: 1 # comment', {'a': 1}, id='a line comment after a value'),
		pytest.param('a: 1#comment', {'a': 1}, id='a line comment with no space'),
		pytest.param('a: 1\n# end', {'a': 1}, id='a line comment at the end without a line feed'),
		pytest.param('#\na: 1', {'a': 1}, id='an empty line comment'),
		pytest.param('a: 1 # it\'s "fine"', {'a': 1}, id='a line comment with quotes'),
		pytest.param('a: 1 # { [ ,', {'a': 1}, id='a line comment with braces'),
		pytest.param('a: 1 # 日本語 😀 \u2028', {'a': 1}, id='a line comment with non-ASCII text'),
		pytest.param('/* comment */\na: 1', {'a': 1}, id='a block comment'),
		pytest.param('/*\n\tline one\n\tline two\n*/\na: 1', {'a': 1}, id='a multi-line block comment'),
		pytest.param('/**/a: 1', {'a': 1}, id='an empty block comment'),
		pytest.param('/*** stars ***/\na: 1', {'a': 1}, id='a block comment with stars'),
		pytest.param('/* a/b */\na: 1', {'a': 1}, id='a block comment with a slash inside'),
		pytest.param('/*/ x */\na: 1', {'a': 1}, id='a block comment starting with a slash'),
		pytest.param('/* # */\na: 1', {'a': 1}, id='a block comment with a hash'),
		pytest.param('/* a/*/\na: 1', {'a': 1}, id='a block comment whose body ends with a slash'),
		pytest.param('/*/*/\na: 1', {'a': 1}, id='a block comment that is only a slash'),
		pytest.param('a: 1 /* x/*/ # y', {'a': 1}, id='a block comment whose body ends with a slash, after a value'),
		pytest.param('a: /* x */ 1', {'a': 1}, id='a block comment between a colon and a value'),
		pytest.param('a: /* x\n */ 1', {'a': 1}, id="a block comment across the colon's line break"),
		pytest.param('a: 1 /* x */\nb: 2', {'a': 1, 'b': 2}, id='a block comment after a value'),
		pytest.param('a: 1 /* x\ny */\nb: 2', {'a': 1, 'b': 2}, id='a multi-line block comment after a value'),
		pytest.param('a: 1/* comment */', {'a': 1}, id='a block comment directly after a number'),
		pytest.param('[1/* comment */,2]', [1, 2], id='a block comment before a comma'),
		pytest.param('[\n\t1, # one\n\t/* two */ 2,\n\t# nothing\n]', [1, 2], id='comments inside an array'),
		pytest.param('{\n\ta: 1, # one\n\t/* b */ b: 2,\n}', {'a': 1, 'b': 2}, id='comments inside an object'),
		pytest.param('[1 /* x */, 2 /* y\n */, 3]', [1, 2, 3], id='comments before a comma'),
		pytest.param('{ # nothing\n}', {}, id='a comment inside an empty object'),
		pytest.param('[ /* nothing */ ]', [], id='a comment inside an empty array'),
		pytest.param('# start\n{a: 1}\n# end\n', {'a': 1}, id='comments around the document'),
		pytest.param('a: 1\n# x\n\n# y\nb: 2', {'a': 1, 'b': 2}, id='comment-only lines between entries'),
		pytest.param('a: {b: 1} # c', {'a': {'b': 1}}, id='a comment after an object value'),
	],
)
def test_comments_are_not_content(source: str, expected: soml.Document) -> None:
	assert typed(soml.loads(source)) == typed(expected)


# Keys


@pytest.mark.parametrize(
	'key',
	[
		'a',
		'name',
		'content-type',
		'x-forwarded-for',
		'_private',
		'__dunder__',
		'404',
		'-foo',
		'--flag',
		'2024-01-01',
		'7zip-bin',
		'a_b-c',
		'UPPER',
		'camelCase',
		'0x1F',
		'1e10',
		'-1',
		'-',
		'_',
		'true',
		'false',
		'null',
		'infinity',
		'-infinity',
		'nan',
		'inf',
		'1h30m',
	],
)
def test_a_bare_key_is_a_string_and_is_never_coerced(key: str) -> None:
	assert typed(soml.loads(f'{key}: 1')) == typed({key: 1})


@pytest.mark.parametrize(
	('source', 'key'),
	[
		("'the name'", 'the name'),
		('"the name"', 'the name'),
		("'a.b'", 'a.b'),
		("''", ''),
		('""', ''),
		("'key: with colon'", 'key: with colon'),
		("'#hash'", '#hash'),
		("'日本'", '日本'),
		('"\\u{61}"', 'a'),
		('"tab\\there"', 'tab\there'),
		('"line\\nbreak"', 'line\nbreak'),
		('"\\u{a}"', '\n'),
		("'  padded  '", '  padded  '),
		("'C:\\dir'", 'C:\\dir'),
		('"it\'s"', "it's"),
		("'{}'", '{}'),
	],
)
def test_a_quoted_key(source: str, key: str) -> None:
	assert list(load_object(f'{source}: 1')) == [key]


def test_quoted_keys_work_inside_braces_too() -> None:
	assert typed(soml.loads('{\'a b\': 1, "c.d": 2}')) == typed({'a b': 1, 'c.d': 2})


def test_a_quoted_key_may_hold_a_line_feed_written_as_an_escape() -> None:
	assert typed(soml.loads(r'"a\nb": 1')) == typed({'a\nb': 1})
	assert typed(soml.loads(r'a: {"b\u{a}c": 1}')) == typed({'a': {'b\nc': 1}})
	assert typed(soml.loads(r'{"\n": 1}')) == typed({'\n': 1})


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		pytest.param("'a.b.c': 1", {'a.b.c': 1}, id='literal'),
		pytest.param('"a.b": 1', {'a.b': 1}, id='escaped'),
		pytest.param("'example.com': 1", {'example.com': 1}, id='a host name'),
		pytest.param("'3.14': 'x'", {'3.14': 'x'}, id='a number'),
		pytest.param("'1.2.3': 'x'", {'1.2.3': 'x'}, id='a version'),
		pytest.param("'.': 1", {'.': 1}, id='only a dot'),
		pytest.param("[{'a.b': 1}, {'a.b': 2}]", [{'a.b': 1}, {'a.b': 2}], id='in array items'),
	],
)
def test_a_quoted_key_with_dots_is_one_key(source: str, expected: soml.Document) -> None:
	assert typed(soml.loads(source)) == typed(expected)


def test_a_quoted_key_with_a_dot_does_not_collide_with_a_nested_key() -> None:
	assert typed(soml.loads("'a.b': 1\na: {b: 2}")) == typed({'a.b': 1, 'a': {'b': 2}})


@pytest.mark.parametrize('second', ['a', "'a'", '"a"', r'"\u{61}"'])
def test_duplicate_keys_are_decided_on_the_decoded_key(second: str) -> None:
	error = decode_error(f'a: 1\n{second}: 2')
	assert (error.msg, error.lineno, error.colno) == ('Duplicate key a', 2, 1)


def test_duplicate_keys_do_not_normalize_unicode() -> None:
	assert list(load_object('"\\u{e9}": 1\n"e\\u{301}": 2')) == ['\u00e9', 'e\u0301']
	assert list(load_object("'\u00e9': 1\n'e\u0301': 2")) == ['\u00e9', 'e\u0301']


def test_keys_are_case_sensitive() -> None:
	assert typed(soml.loads('a: 1\nA: 2')) == typed({'a': 1, 'A': 2})


def test_a_dash_and_an_underscore_are_different_keys() -> None:
	assert len(load_object('transfer-encoding: 1\ntransfer_encoding: 2')) == 2


# Strings


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		("''", ''),
		("'a'", 'a'),
		("'hello world'", 'hello world'),
		(r"'C:\Users\sindre\dev'", r'C:\Users\sindre\dev'),
		(r"'^\d{4}-\d{2}-\d{2}$'", r'^\d{4}-\d{2}-\d{2}$'),
		(r"'C:\Users\n\t\u{41}'", r'C:\Users\n\t\u{41}'),
		('\'say "hi"\'', 'say "hi"'),
		("'\\'", '\\'),
		("'tab\there'", 'tab\there'),
		("'# not a comment'", '# not a comment'),
		("'/* not a comment */'", '/* not a comment */'),
		("'/- not removed'", '/- not removed'),
		("'a: b, c: d'", 'a: b, c: d'),
		("'{[]}'", '{[]}'),
		("'日本語'", '日本語'),
		("'😀👍🏽'", '😀👍🏽'),
		("'e\u0301'", 'e\u0301'),
		("'\u2028'", '\u2028'),
		("'\u00a0'", '\u00a0'),
		("'\ufeff'", '\ufeff'),
		("'   '", '   '),
	],
)
def test_a_literal_string_has_no_escapes(source: str, expected: str) -> None:
	assert load_value(source) == expected


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('""', ''),
		('"a"', 'a'),
		(r'"\\"', '\\'),
		(r'"\""', '"'),
		(r'"\n"', '\n'),
		(r'"\t"', '\t'),
		(r'"a\nb\tc"', 'a\nb\tc'),
		(r'"\u{0}"', '\x00'),
		(r'"\u{9}"', '\t'),
		(r'"\u{a}"', '\n'),
		(r'"\u{41}"', 'A'),
		(r'"\u{e9}"', 'é'),
		(r'"\u{7f}"', '\x7f'),
		(r'"\u{ff}"', 'ÿ'),
		(r'"\u{2028}"', '\u2028'),
		(r'"\u{fffe}"', '\ufffe'),
		(r'"\u{ffff}"', '\uffff'),
		(r'"\u{10000}"', '\U00010000'),
		(r'"\u{1f600}"', '😀'),
		(r'"\u{10ffff}"', '\U0010ffff'),
		(r'"\u{d7ff}"', '\ud7ff'),
		(r'"\u{e000}"', '\ue000'),
		('"it\'s"', "it's"),
		('"default-src \'self\'"', "default-src 'self'"),
		(r'"\\\\server\\share"', r'\\server\share'),
		(r'"a\u{301}"', 'a\u0301'),
		(r'"\u{1f1f3}\u{1f1f4}"', '🇳🇴'),
		('"raw\ttab"', 'raw\ttab'),
		('"日本語"', '日本語'),
		(r'"\\n"', r'\n'),
		(r'"\\u{41}"', r'\u{41}'),
		(r'"\\\"\n\t\u{0}\u{41}\u{1f600}"', '\\"\n\t\x00A😀'),
	],
)
def test_an_escaped_string_decodes_its_escapes(source: str, expected: str) -> None:
	assert load_value(source) == expected


@pytest.mark.parametrize('code_point', [*range(0x0D), *range(0x0E, 0x20), 0x7F])
def test_every_control_character_but_cr_is_representable_as_an_escape_in_a_value_and_in_a_key(code_point: int) -> None:
	escape = f'\\u{{{code_point:x}}}'
	assert soml.loads(f'a: "{escape}"') == {'a': chr(code_point)}
	assert soml.loads(f'"{escape}": 1') == {chr(code_point): 1}


def test_strings_are_compared_by_code_points_without_normalization() -> None:
	composed = load_value("'\u00e9'")
	decomposed = load_value("'e\u0301'")
	assert composed != decomposed
	assert load_value(r'"\u{e9}"') == composed
	assert load_value(r'"e\u{301}"') == decomposed


def test_a_long_escaped_string_with_many_escapes() -> None:
	assert load_value(f'"{r"x\n" * 10_000}"') == 'x\n' * 10_000


def test_a_long_literal_string() -> None:
	text = 'é' * 100_000
	assert load_value(f"'{text}'") == text


def test_a_very_long_string() -> None:
	content = 'abcdefghij' * 500_000
	assert load_value(f"'{content}'") == content
	assert load_value(f'"{content}"') == content


def test_a_string_with_many_escapes() -> None:
	value = load_value(f'"{r"\n\t\u{1f600}\\" * 50_000}"')
	assert value == '\n\t😀\\' * 50_000


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		pytest.param("a: '''\n\t\tone\n\t\t\ttwo\n\t\t'''", 'one\n\ttwo', id='dedent by the closing delimiter'),
		pytest.param("a: '''\n\n\n\tx\n\n\ty\n\n\n\t'''", 'x\n\ny', id='blank lines are dropped at the edges only'),
		pytest.param("a: '''\n    \n  x\n  '''", 'x', id='a leading whitespace-only line indented more is discarded'),
		pytest.param("a: '''\n  x\n     \n  '''", 'x', id='a trailing whitespace-only line indented more is discarded'),
		pytest.param("a: '''\n  x\n \n  y\n  '''", 'x\n\ny', id='an inner whitespace-only line indented less becomes empty'),
		pytest.param("a: '''\n\tx\n \t \n\ty\n\t'''", 'x\n\ny', id='an inner line of spaces and tabs becomes empty'),
		pytest.param('a: """\n\tx\n\t\t\n\ty\n\t"""', 'x\n\ny', id='an inner whitespace-only line in an escaped block becomes empty'),
		pytest.param('a: """\n\t\\t\\u{41}\n\t"""', '\tA', id='escapes are decoded after dedenting'),
		pytest.param("a: '''''\n\t'''\n\t''''\n\t'''''", "'''\n''''", id='a longer delimiter can hold the shorter runs'),
		pytest.param("a: '''\n\tone\n\ttwo\n\t'''", 'one\ntwo', id='a literal block'),
		pytest.param('a: """\n\tone\\ttwo\n\tthree\\u{21}\n\t"""', 'one\ttwo\nthree!', id='an escaped block'),
		pytest.param('a: """\n\t\\\\\n\t"""', '\\', id='an escaped block keeps backslashes from escapes'),
		pytest.param("a: '''\n\tC:\\dev\\n\n\t'''", 'C:\\dev\\n', id='a literal block keeps backslashes'),
		pytest.param("a: '''\none\ntwo\n'''", 'one\ntwo', id='no indentation'),
		pytest.param("a: '''\n    one\n    two\n    '''", 'one\ntwo', id='space indentation'),
		pytest.param("a: '''\n\t\tone\n\t'''", '\tone', id='a closing delimiter less indented than the content'),
		pytest.param("a: '''\n'''", '', id='an empty block'),
		pytest.param("a: '''\n\t'''", '', id='an empty block with indentation'),
		pytest.param("a: '''\n\n\n'''", '', id='only blank lines'),
		pytest.param("a: '''\n\t\n  \n \t \n\t'''", '', id='only whitespace-only lines'),
		pytest.param("a: '''\n\t\n\tone\n\t\n\t'''", 'one', id='a line with only the indentation is blank'),
		pytest.param("a: '''\n\tone\n\t  \n\ttwo\n\t'''", 'one\n\ntwo', id='a whitespace-only line with more than the indentation becomes empty'),
		pytest.param("a: '''\n\tone  \n\t'''", 'one  ', id='trailing whitespace is content'),
		pytest.param("a: '''\n\tit's\n\t'''", "it's", id='a single quote inside'),
		pytest.param("a: '''\n\t''\n\t'''", "''", id='two single quotes inside'),
		pytest.param("a: '''\n\tsay ''' here\n\t'''", "say ''' here", id='three quotes mid-line are content'),
		pytest.param("a: '''\n\t''''\n\t'''", "''''", id='four quotes at a line start are content for a three-quote block'),
		pytest.param("a: '''\n\t\"\"\"\n\t'''", '"""', id='double quotes inside a literal block'),
		pytest.param('a: """\n\t\'\'\'\n\t"""', "'''", id='single quotes inside an escaped block'),
		pytest.param("a: '''\n\t# not a comment\n\t/* nor this */\n\t'''", '# not a comment\n/* nor this */', id='comment characters are content'),
		pytest.param("a: '''\n\t日本語\n\t😀\n\t'''", '日本語\n😀', id='non-ASCII content'),
		pytest.param('a: """\n\tone\\ntwo\n\t"""', 'one\ntwo', id='an escaped line feed in an escaped block'),
		pytest.param('a: """\n\t\\"""\n\t"""', '"""', id='an escaped quote run in an escaped block'),
	],
)
def test_parses_a_block_string(source: str, expected: str) -> None:
	assert load_object(source)['a'] == expected


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		("[\n\t'''\n\tx\n\t''',\n]", ['x']),
		("[\n\t'''\n\tx\n\t'''\n]", ['x']),
		("[\n\t'''\n\tx\n\t'''  ,\n]", ['x']),
		("[\n\t'''\n\tx\n\t'''] ", ['x']),
		("a: '''\n\tx\n\t''' # comment\nb: 1", {'a': 'x', 'b': 1}),
		("a: '''\n\tx\n\t'''\nb: 1", {'a': 'x', 'b': 1}),
		("{a: '''\n\tx\n\t''', b: 1}", {'a': 'x', 'b': 1}),
		('items: [\n\t\'\'\'\n\tone\n\t\'\'\',\n\t"""\n\ttwo\\n\n\t""",\n]', {'items': ['one', 'two\n']}),
		("outer: {\n\tinner: [\n\t\t{\n\t\t\ttext: '''\n\t\t\t\tdeep\n\t\t\t\t'''\n\t\t},\n\t],\n}", {'outer': {'inner': [{'text': 'deep'}]}}),
	],
)
def test_a_block_string_can_be_followed_by_a_comma_a_comment_or_a_closing_bracket(source: str, expected: soml.Document) -> None:
	assert typed(soml.loads(source)) == typed(expected)


def test_a_block_string_with_many_lines() -> None:
	lines = '\n'.join(f'\tline {index}' for index in range(50_000))
	value = load_object(f"a: '''\n{lines}\n\t'''")['a']
	assert isinstance(value, str)
	assert value.startswith('line 0\nline 1\n')
	assert value.endswith('line 49999')


def test_a_block_string_with_many_blank_lines_at_its_edges() -> None:
	blank = '\n' * 100_000
	assert load_object(f"a: '''{blank}\tx{blank}\t'''")['a'] == 'x'


# Numbers


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('0', 0),
		('1', 1),
		('-1', -1),
		('42', 42),
		('1_000', 1000),
		('1_000_000', 1_000_000),
		('1_2_3', 123),
		('-1_000', -1000),
		('9223372036854775807', 2**63 - 1),
		('-9223372036854775808', -(2**63)),
		('9_223_372_036_854_775_807', 2**63 - 1),
		('0x0', 0),
		('0xFF', 255),
		('0x00FF', 255),
		('0xDEAD_BEEF', 0xDEAD_BEEF),
		('0x7FFFFFFFFFFFFFFF', 2**63 - 1),
		('0x7FFF_FFFF_FFFF_FFFF', 2**63 - 1),
		('0o0', 0),
		('0o17', 15),
		('0o644', 0o644),
		('0o007', 7),
		('0o777_777', 0o777_777),
		('0o777777777777777777777', 2**63 - 1),
		('0b0', 0),
		('0b1', 1),
		('0b1010', 10),
		('0b1010_1100', 0b1010_1100),
		('0b00001010', 10),
		(f'0b{"1" * 63}', 2**63 - 1),
		('10', 10),
		('100', 100),
		('1234567890', 1_234_567_890),
	],
)
def test_parses_an_int(source: str, expected: int) -> None:
	assert typed(load_value(source)) == typed(expected)


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('0.0', 0.0),
		('0.5', 0.5),
		('1.5', 1.5),
		('-1.5', -1.5),
		('3.0', 3.0),
		('30.0', 30.0),
		('1e10', 1e10),
		('1e-10', 1e-10),
		('-1e10', -1e10),
		('-1e-10', -1e-10),
		('1.5e3', 1500.0),
		('1.5e-3', 0.0015),
		('100e2', 10_000.0),
		('0e0', 0.0),
		('0.0e0', 0.0),
		('1_000.5', 1000.5),
		('1.000_001', 1.000001),
		('1_0.5_0', 10.5),
		('1e1_0', 1e10),
		('1e0', 1.0),
		('1e5', 1e5),
		('1e-5', 1e-5),
		('1.5e10', 1.5e10),
		('0.1', 0.1),
		('0.2', 0.2),
		('0.30000000000000004', 0.30000000000000004),
		('3.141592653589793', math.pi),
		('1.7976931348623157e308', sys.float_info.max),
		('-1.7976931348623157e308', -sys.float_info.max),
		('1.7976931348623158e308', sys.float_info.max),
		('5e-324', 5e-324),
		('2.2250738585072014e-308', sys.float_info.min),
		('2.2250738585072011e-308', 2.225073858507201e-308),
		('9007199254740993.0', 9_007_199_254_740_992.0),
		('9007199254740995.0', 9_007_199_254_740_996.0),
		('9007199254740993.0000001', 9_007_199_254_740_994.0),
		('123456789012345678901234567890.0', 123456789012345678901234567890.0),
		('0.000000000000000000000000000001', 1e-30),
	],
)
def test_parses_a_float(source: str, expected: float) -> None:
	assert typed(load_value(source)) == typed(expected)


def test_a_float_with_many_digits_rounds_correctly() -> None:
	assert load_value(f'0.{"3" * 1000}') == 1 / 3
	assert load_value(f'0.{"3" * 100_000}') == 1 / 3
	assert load_value(f'1{"0" * 300}.0') == 1e300


def test_a_long_float_still_rounds_correctly() -> None:
	zeros = '0' * 20_000
	# 2^53 + 1 is halfway between two floats, so it rounds to the even one, unless anything after it is nonzero.
	assert load_value(f'9007199254740993.{zeros}') == 9_007_199_254_740_992.0
	assert load_value(f'9007199254740993.{zeros}1') == 9_007_199_254_740_994.0
	assert load_value(f'-9007199254740993.{zeros}1') == -9_007_199_254_740_994.0
	assert load_value(f'0.{zeros}1e20005') == 1e4
	assert load_value(f'1{zeros}.0e-20000') == 1.0


@pytest.mark.parametrize('zero_count', [700, 753, 768, 769, 800, 1500])
def test_a_halfway_value_with_many_trailing_zeros_rounds_to_even(zero_count: int) -> None:
	zeros = '0' * zero_count
	assert load_value(f'9007199254740993.{zeros}') == 9_007_199_254_740_992.0
	assert load_value(f'9007199254740995.{zeros}') == 9_007_199_254_740_996.0
	assert load_value(f'9007199254740993.{zeros}1') == 9_007_199_254_740_994.0
	assert load_value(f'9007199254740993{zeros}e-{zero_count}') == 9_007_199_254_740_992.0


def test_an_exponent_of_zero_has_one_spelling() -> None:
	assert typed(load_value('1e0')) == typed(1.0)
	error = decode_error('a: 1e-0')
	assert (error.msg, error.colno) == ('“e-0” is not allowed, because an exponent of zero has one spelling: e0', 4)
	assert decode_error('a: 1.5e-0').msg == '“e-0” is not allowed, because an exponent of zero has one spelling: e0'


@pytest.mark.parametrize('text', ['1e05', '1e-05', '1e00', '1e0_5', '1.5e007', '-1e-0_1'])
def test_an_exponent_has_no_leading_zeros(text: str) -> None:
	error = decode_error(f'a: {text}')
	assert (error.msg, error.colno) == ('Leading zeros are not allowed in an exponent', 4)


@pytest.mark.parametrize('text', ['0x8000000000000000', '0o1000000000000000000000', f'0b1{"0" * 63}', '9223372036854775808', '-9223372036854775809', '0xFFFFFFFFFFFFFFFF', '0x1_0000_0000_0000_0000'])
def test_an_int_outside_int64_is_rejected_in_every_radix(text: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: {text}')


def test_a_huge_int_does_not_hang() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: {"9" * 100_000}')


def test_the_first_float_past_the_largest_finite_one_is_too_large() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='too large'):
		soml.loads('a: 1.7976931348623159e308')


def test_a_huge_exponent_is_too_large() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='too large'):
		soml.loads(f'a: 1e{"9" * 1_000_000}')


def test_random_floats_read_back_from_their_repr() -> None:
	generator = random.Random(3)  # A seeded generator, so a failure can be reproduced.

	for _ in range(5000):
		value = generator.uniform(-1e6, 1e6) * 10 ** generator.randint(-300, 300)

		if not math.isfinite(value) or value == 0:
			continue

		text = repr(value).replace('e+', 'e').replace('e-0', 'e-')
		text = text if '.' in text or 'e' in text else f'{text}.0'
		assert load_value(text) == value, text


# Durations


@pytest.mark.parametrize(
	('unit', 'expected'),
	[
		('h', timedelta(hours=7)),
		('m', timedelta(minutes=7)),
		('s', timedelta(seconds=7)),
		('ms', timedelta(milliseconds=7)),
		('us', timedelta(microseconds=7)),
	],
)
def test_every_unit_in_its_own_length(unit: str, expected: timedelta) -> None:
	assert load_value(f'7{unit}') == expected


def test_nanoseconds_that_are_whole_microseconds_are_accepted() -> None:
	assert load_value('7000ns') == timedelta(microseconds=7)
	assert load_value('1_000_000ns') == timedelta(milliseconds=1)


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('0s', timedelta(0)),
		('0ns', timedelta(0)),
		('0h0m', timedelta(0)),
		('0.0s', timedelta(0)),
		('250us', timedelta(microseconds=250)),
		('10ms', timedelta(milliseconds=10)),
		('30s', timedelta(seconds=30)),
		('1h30m', timedelta(minutes=90)),
		('90m', timedelta(minutes=90)),
		('5400s', timedelta(minutes=90)),
		('1500ms', timedelta(milliseconds=1500)),
		('1_000ms', timedelta(seconds=1)),
		('1h0m', timedelta(hours=1)),
		('1m1ms', timedelta(minutes=1, milliseconds=1)),
		('1h2m3s4ms5us', timedelta(hours=1, minutes=2, seconds=3, milliseconds=4, microseconds=5)),
		('24h', timedelta(hours=24)),
		('8760h', timedelta(hours=8760)),
		('-5m', timedelta(minutes=-5)),
		('-1h30m', timedelta(minutes=-90)),
		('-90m', timedelta(minutes=-90)),
		('1.5s', timedelta(milliseconds=1500)),
		('1.50s', timedelta(milliseconds=1500)),
		('1.0s', timedelta(seconds=1)),
		('1.500000000000000000000s', timedelta(milliseconds=1500)),
		(f'1.5{"0" * 5000}s', timedelta(milliseconds=1500)),
		('1.5h', timedelta(minutes=90)),
		('0.1h', timedelta(minutes=6)),
		('1h1.5m', timedelta(hours=1, minutes=1, seconds=30)),
		('1h0.5ms', timedelta(hours=1, microseconds=500)),
		('2.5ms', timedelta(microseconds=2500)),
		('-0.25s', timedelta(milliseconds=-250)),
		('0.000001s', timedelta(microseconds=1)),
		('59m59.999999s', timedelta(minutes=59, seconds=59, microseconds=999_999)),
		('9223372036854775000ns', timedelta(microseconds=9_223_372_036_854_775)),
		('2562047h47m16.854775s', timedelta(microseconds=9_223_372_036_854_775)),
		('-9223372036854775000ns', timedelta(microseconds=-9_223_372_036_854_775)),
		('-2562047h47m16.854775s', timedelta(microseconds=-9_223_372_036_854_775)),
	],
)
def test_parses_a_duration(source: str, expected: timedelta) -> None:
	assert typed(load_value(source)) == typed(expected)


def test_the_int64_bounds_are_checked_before_the_microsecond_limit() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='outside the 64-bit range'):
		soml.loads('a: 9223372036854775808ns')

	with pytest.raises(soml.SOMLDecodeError, match='outside the 64-bit range'):
		soml.loads('a: -9223372036854775809ns')


def test_a_fraction_finer_than_a_nanosecond_is_not_a_whole_number_of_nanoseconds() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='not a whole number of nanoseconds'):
		soml.loads('a: 0.0000000000001h')

	with pytest.raises(soml.SOMLDecodeError, match='not a whole number of nanoseconds'):
		soml.loads('a: 0.5ns')


def test_a_duration_in_containers_and_before_a_comment() -> None:
	assert soml.loads('a: [1s, 2m,]') == {'a': [timedelta(seconds=1), timedelta(minutes=2)]}
	assert soml.loads('{a: 1s}') == {'a': timedelta(seconds=1)}
	assert soml.loads('{a:1s,b:2s}') == {'a': timedelta(seconds=1), 'b': timedelta(seconds=2)}
	assert soml.loads('a: 1s # one second') == {'a': timedelta(seconds=1)}
	assert soml.loads('a: 1s/* one second */') == {'a': timedelta(seconds=1)}


def test_a_duration_shaped_key_is_a_string() -> None:
	assert soml.loads('1h30m: 1h30m') == {'1h30m': timedelta(minutes=90)}


# Instants


@pytest.mark.parametrize('source', ['2024-02-29T00:00:00Z', '2000-02-29T00:00:00Z', '2400-02-29T00:00:00Z', '0004-02-29T00:00:00Z'])
def test_february_29_in_a_leap_year(source: str) -> None:
	assert load_value(source) == datetime.fromisoformat(source)


@pytest.mark.parametrize('source', ['1900-02-29T00:00:00Z', '2100-02-29T00:00:00Z', '2023-02-29T00:00:00Z'])
def test_february_29_in_a_common_year(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='the day must be 01 to 28 in that month'):
		soml.loads(f'a: {source}')


@pytest.mark.parametrize('year', [2023, 2024])
@pytest.mark.parametrize('month', range(1, 13))
def test_every_month_length(year: int, month: int) -> None:
	lengths = [31, 29 if year == 2024 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
	last = lengths[month - 1]
	assert load_value(f'{year}-{month:02}-{last}T00:00:00Z') == datetime(year, month, last, tzinfo=UTC)

	with pytest.raises(soml.SOMLDecodeError, match=f'day must be 01 to {last}'):
		soml.loads(f'a: {year}-{month:02}-{last + 1}T00:00:00Z')


def test_every_day_of_a_year_is_one_day_after_the_previous() -> None:
	for year in [2023, 2024]:
		previous = load_value(f'{year}-01-01T00:00:00Z')
		assert isinstance(previous, datetime)

		for day in range(1, 366 if year == 2024 else 365):
			current = load_value(f'{(datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=day)).strftime("%Y-%m-%d")}T00:00:00Z')
			assert isinstance(current, datetime)
			assert current - previous == timedelta(days=1)
			previous = current


def test_the_range_is_checked_in_utc() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='outside the years 0001 to 9999'):
		soml.loads('a: 0001-01-01T00:59:59+01:00')

	assert load_value('0001-01-01T01:00:00+01:00') == datetime(1, 1, 1, tzinfo=UTC)

	with pytest.raises(soml.SOMLDecodeError, match='outside the years 0001 to 9999'):
		soml.loads('a: 9999-12-31T23:00:00-01:00')

	assert load_value('9999-12-31T22:59:59.999999-01:00') == datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)


def test_the_edges_of_the_range_do_not_overflow() -> None:
	assert load_value('0001-01-01T00:00:00Z') == datetime.min.replace(tzinfo=UTC)
	assert load_value('9999-12-31T23:59:59.999999Z') == datetime.max.replace(tzinfo=UTC)

	with pytest.raises(soml.SOMLDecodeError, match='outside the years 0001 to 9999'):
		soml.loads('a: 0001-01-01T00:00:00+00:01')

	with pytest.raises(soml.SOMLDecodeError, match='outside the years 0001 to 9999'):
		soml.loads('a: 9999-12-31T23:59:59-00:01')


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('2026-09-19T14:00:00Z', datetime(2026, 9, 19, 14, tzinfo=UTC)),
		('2026-09-19T14:00:00+00:00', datetime(2026, 9, 19, 14, tzinfo=UTC)),
		('2026-09-19T16:00:00+02:00', datetime(2026, 9, 19, 14, tzinfo=UTC)),
		('2026-09-19T08:30:00-05:30', datetime(2026, 9, 19, 14, tzinfo=UTC)),
		('2026-09-19T14:00:00.5Z', datetime(2026, 9, 19, 14, 0, 0, 500_000, tzinfo=UTC)),
		('2026-09-19T14:00:00.500Z', datetime(2026, 9, 19, 14, 0, 0, 500_000, tzinfo=UTC)),
		('2026-09-19T14:00:00.000Z', datetime(2026, 9, 19, 14, tzinfo=UTC)),
		('2026-09-19T14:00:00.000001Z', datetime(2026, 9, 19, 14, 0, 0, 1, tzinfo=UTC)),
		('2026-09-19T14:00:00.120Z', datetime(2026, 9, 19, 14, 0, 0, 120_000, tzinfo=UTC)),
		('2026-01-01T00:30:00+01:00', datetime(2025, 12, 31, 23, 30, tzinfo=UTC)),
		('2025-12-31T23:30:00-01:00', datetime(2026, 1, 1, 0, 30, tzinfo=UTC)),
		('2024-03-01T00:00:00+00:01', datetime(2024, 2, 29, 23, 59, tzinfo=UTC)),
		('1970-01-01T00:00:00Z', datetime(1970, 1, 1, tzinfo=UTC)),
		('1969-12-31T23:59:59.999999Z', datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)),
		('0001-01-01T00:00:00-01:00', datetime(1, 1, 1, 1, tzinfo=UTC)),
		('9999-12-31T22:59:59+01:00', datetime(9999, 12, 31, 21, 59, 59, tzinfo=UTC)),
		('2026-09-19T14:00:00+23:59', datetime(2026, 9, 18, 14, 1, tzinfo=UTC)),
		('2026-09-19T14:00:00-23:59', datetime(2026, 9, 20, 13, 59, tzinfo=UTC)),
		('1582-10-10T00:00:00Z', datetime(1582, 10, 10, tzinfo=UTC)),
		('1900-03-01T00:00:00Z', datetime(1900, 3, 1, tzinfo=UTC)),
	],
)
def test_parses_an_instant_and_normalizes_it_to_utc(source: str, expected: datetime) -> None:
	value = load_value(source)
	assert value == expected
	assert isinstance(value, datetime)
	assert value.tzinfo is UTC


@pytest.mark.parametrize(
	'source',
	[
		'2026-09-19t14:00:00Z',
		'2026-09-19T14:00:00z',
		'2026-09-19T14:00:00+0700',
		'2026-09-19T14:00:00+02',
		'2026-12-31T23:59:60Z',
		'2026-09-19T14:00:00.1234567891Z',
		'2026-09-19T14:00:00-00:00',
		'2026-09-19T14:00:00',
		'2026-09-19T14:00Z',
		'2026-09-19T14:00:00.Z',
		'2026-01-01T24:00:00Z',
		'2026-W38-6T00:00:00Z',
		'2026-262T00:00:00Z',
		'2026-9-19T14:00:00Z',
		'12026-09-19T14:00:00Z',
		'2026-01-01T00:00:00+24:00',
		'2026-01-01T00:00:00+00:60',
		'2026-01-01T00:00:00Zx',
		'2026-01-01T00:00:00Z+01:00',
	],
)
def test_the_leniency_of_other_date_parsers_is_not_inherited(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'a: {source}')


# Nesting limit


def test_nesting_up_to_the_limit_is_accepted() -> None:
	value: soml.Value = soml.loads('[' * MAX_DEPTH + ']' * MAX_DEPTH)
	depth = 0

	while isinstance(value, list):
		depth += 1
		value = value[0] if value else None

	assert depth == MAX_DEPTH


def test_nesting_past_the_limit_is_a_decode_error_not_a_recursion_error() -> None:
	error = decode_error('[' * (MAX_DEPTH + 1) + ']' * (MAX_DEPTH + 1))
	assert error.msg == f'The document is nested more than {MAX_DEPTH} levels deep'
	assert error.colno == MAX_DEPTH + 1


def test_nested_objects_count_the_same_way_with_the_bare_object_as_a_level() -> None:
	assert list(load_object('a: {' * (MAX_DEPTH - 1) + '}' * (MAX_DEPTH - 1))) == ['a']

	with pytest.raises(soml.SOMLDecodeError, match=f'nested more than {MAX_DEPTH}'):
		soml.loads('a: {' * MAX_DEPTH + '}' * MAX_DEPTH)


def test_braced_and_bare_top_level_objects_hit_the_limit_at_the_same_depth() -> None:
	inner = '{a: ' * (MAX_DEPTH - 1) + '1' + '}' * (MAX_DEPTH - 1)
	assert isinstance(soml.loads(f'a: {inner}'), dict)
	assert isinstance(soml.loads(f'{{a: {inner}}}'), dict)

	deeper = '{a: ' * MAX_DEPTH + '1' + '}' * MAX_DEPTH

	with pytest.raises(soml.SOMLDecodeError, match=f'nested more than {MAX_DEPTH}'):
		soml.loads(f'a: {deeper}')

	with pytest.raises(soml.SOMLDecodeError, match=f'nested more than {MAX_DEPTH}'):
		soml.loads(f'{{a: {deeper}}}')


def test_a_very_long_key_with_dots_is_an_error_not_a_crash() -> None:
	error = decode_error(f'{".".join(["a"] * 100_000)}: 1')
	assert (error.msg, error.colno) == ('A key cannot contain “.” unless it is quoted. Quote the whole key, or use braces to nest, as in a: {b: …}', 2)


def test_an_unclosed_deep_document_fails_at_the_nesting_limit() -> None:
	with pytest.raises(soml.SOMLDecodeError, match=f'nested more than {MAX_DEPTH}'):
		soml.loads('[' * 100_000)

	with pytest.raises(soml.SOMLDecodeError, match=f'nested more than {MAX_DEPTH}'):
		soml.loads('a: ' + '{a: ' * 100_000)


def test_an_unclosed_document_at_the_nesting_limit_is_unterminated() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Unterminated array'):
		soml.loads('[' * MAX_DEPTH)


def test_a_chain_of_missing_values_fails_on_the_first_one() -> None:
	error = decode_error('a: ' * 100_000)
	# A word after the `:` of a member and a space is that member's value, an unquoted string, as in `msg: Error: file not found`.
	assert (error.msg, error.colno) == ("Unexpected “a”. A string value must be quoted, as in 'a: a: a: a: a: a: a: a: a: a: a: a: a: a…'", 4)


def test_deep_nesting_repeated_many_times() -> None:
	deep = '[' * (MAX_DEPTH - 1) + ']' * (MAX_DEPTH - 1)
	value = soml.loads('[' + ','.join([deep] * 1000) + ']')
	assert len(value) == 1000


def test_what_parses_at_the_limit_also_serializes() -> None:
	text = '[' * MAX_DEPTH + ']' * MAX_DEPTH
	assert soml.loads(soml.dumps(soml.loads(text))) == soml.loads(text)


# Large and hostile input


def test_an_object_with_many_keys() -> None:
	value = load_object('\n'.join(f'k{index}: {index}' for index in range(100_000)))
	assert len(value) == 100_000
	assert typed(value['k77777']) == typed(77777)


def test_a_duplicate_at_the_end_of_a_large_object() -> None:
	source = '\n'.join(f'k{index}: {index}' for index in range(100_000)) + '\nk0: 1'

	with pytest.raises(soml.SOMLDecodeError, match='Duplicate key k0'):
		soml.loads(source)


def test_a_large_array() -> None:
	assert len(soml.loads('[' + '1,' * 500_000 + ']')) == 500_000


def test_many_empty_containers() -> None:
	assert len(soml.loads('[' + '[], {}, ' * 100_000 + ']')) == 200_000


def test_long_combining_sequences_remain_distinct_keys() -> None:
	first = 'x' + '\u0315' * 128_000 + '\u0300' * 128_000
	second = 'x' + '\u0300' * 128_000 + '\u0315' * 128_000
	value = load_object(f"'{first}': 1\n'{second}': 2")
	assert value == {first: 1, second: 2}


def test_a_very_long_comment() -> None:
	comment = 'x' * 1_000_000
	assert soml.loads(f'# {comment}\na: 1') == {'a': 1}
	assert soml.loads(f'/* {comment} */\na: 1') == {'a': 1}


def test_many_comments() -> None:
	assert soml.loads('# comment\n/* block */\n' * 50_000 + 'a: 1') == {'a': 1}


def test_a_huge_valid_float() -> None:
	assert load_value(f'0.{"1" * 1_000_000}') == 0.1111111111111111


def test_a_huge_hexadecimal_int_with_leading_zeros() -> None:
	assert load_value(f'0x{"0" * 1_000_000}FF') == 255


def test_huge_out_of_range_ints() -> None:
	length = 1_000_000

	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: {"1" * length}')

	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: 0x{"F" * length}')

	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: 0b{"1_" * (length // 2)}1')

	with pytest.raises(soml.SOMLDecodeError, match='64-bit range'):
		soml.loads(f'a: {"9" * length}h')


def test_huge_invalid_numbers_instants_and_durations() -> None:
	length = 1_000_000
	sources = [
		f'a: 1{"_" * length}',
		f'a: 1{"x" * length}',
		f'a: 1.{"2" * length}.3',
		f'a: 2026-01-01T00:00:00.{"1" * length}Z',
		f'a: {"x" * length}',
		f'a: -0.{"0" * length}s',
		f'a: 1{"s" * length}',
		f'a: {"1" * length}h1',
		f'a: 1.{"0" * length}1s',
	]

	for source in sources:
		error = decode_error(source)
		assert len(str(error)) < 300


def test_a_huge_bare_key_as_a_duplicate_and_through_dumps() -> None:
	key = 'k' * 1_000_000
	error = decode_error(f'{key}: 1\n{key}: 2')
	assert error.msg == f'Duplicate key {"k" * 40}…'
	assert len(soml.dumps(soml.loads(f'{key}: 1'))) == len(key) + 4


def test_random_bytes_only_ever_raise_a_decode_error() -> None:
	generator = random.Random(1)  # A seeded generator, so a failure can be reproduced.

	for _ in range(20_000):
		data = generator.randbytes(generator.randrange(64))

		with contextlib.suppress(soml.SOMLDecodeError):
			soml.loads(data)


def test_random_punctuation_only_ever_raises_a_decode_error() -> None:
	generator = random.Random(2)  # A seeded generator, so a failure can be reproduced.
	alphabet = '[]{}:,.\'"#/*-\\\n\t 0123abcexT_+'

	for _ in range(50_000):
		text = ''.join(generator.choices(alphabet, k=generator.randrange(48)))

		with contextlib.suppress(soml.SOMLDecodeError):
			soml.loads(text)


# The examples in the readme and the comparison


def test_the_edge_service_example() -> None:
	assert typed(soml.loads(EDGE_SERVICE_DOCUMENT)) == typed(
		{
			'name': 'api-gateway',
			'version': '2.1.0',
			'pattern': r'^\d{4}-\d{2}-\d{2}$',
			'windows-path': r'C:\Users\sindre\dev',
			'content-security-policy': "default-src 'self'; script-src https://cdn.example.com",
			'replicas': 3,
			'timeout': timedelta(seconds=30),
			'drained': False,
			'owner': None,
			'deployed-at': datetime(2026, 9, 19, 14, tzinfo=UTC),
			'labels': ['prod', 'eu-west', 'canary'],
			'description': 'The API gateway terminates TLS.\nIt routes requests to internal services.\nIt has no external dependencies.',
			'postgres': {'host': 'db.internal', 'port': 5432, 'ssl': True},
		}
	)


def test_the_every_feature_example() -> None:
	assert typed(soml.loads(EVERY_FEATURE_DOCUMENT)) == typed(
		{
			'name': 'soml',
			'launch-date': date(2026, 9, 19),
			'released-at': datetime(2026, 9, 19, 14, tzinfo=UTC),
			'the name': 'quotes are the escape hatch, when a key needs spaces',
			'404': 'and a key may begin with a digit',
			'regex': r'^\d{4}-\d{2}-\d{2}$',
			'windows-path': r'C:\Users\sindre\dev',
			'shell': r'echo "$HOME" | tr -d "\n"',
			'escaped': 'line one\nline two\t"quoted"😀',
			'description': 'A block string keeps its line breaks.\nThe leading tab of each line is stripped,\nbecause the closing delimiter sits at one tab.',
			'replicas': 3,
			'ratio': 0.75,
			'budget': 1_000_000,
			'mask': 0b1010_1100,
			'permissions': 0o644,
			'color': 0xFF8800,
			'flags': 0x00FF,
			'limit': math.inf,
			'floor': -math.inf,
			'retry-after': timedelta(seconds=90),
			'drained': False,
			'owner': None,
			'labels': ['prod', 'eu-west', 'canary'],
			'matrix': [[1, 2], [3, 4]],
			'mixed': [1, 'two', True, None],
			'pool': {'min': 2, 'max': 16},
			'maintainers': [
				{'name': 'Sindre Sorhus', 'email': 'sindresorhus@gmail.com'},
				{'name': 'Random Person', 'email': 'person@example.com'},
			],
			'healthcheck': {'path': '/health', 'interval': timedelta(seconds=10), 'grace': timedelta(milliseconds=1500)},
		}
	)


@pytest.mark.parametrize('document', [EDGE_SERVICE_DOCUMENT, EVERY_FEATURE_DOCUMENT], ids=['edge service', 'every feature'])
def test_an_example_reads_back_from_its_canonical_form(document: str) -> None:
	value = soml.loads(document)
	text = soml.dumps(value, canonical=True)
	assert typed(soml.loads(text)) == typed(value)
	assert soml.dumps(soml.loads(text), canonical=True) == text


def test_dir_lists_tree_once() -> None:
	assert soml.tree is not None
	assert dir(soml).count('tree') == 1
