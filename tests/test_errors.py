"""
Unit tests for `SOMLDecodeError`: its attributes, its positions, and its messages. Ported from soml-js's `test/errors.ts` and from the Swift port's rejection tests.

The code frame tests are at the end. Columns count code points, not UTF-16 code units. The Swift port words its messages differently, so its rejection tests check only that the document is rejected.
"""

import copy
import json
import math
import pickle
import random
import re
import traceback
from datetime import date, datetime
from pathlib import Path

import pytest
from helpers import decode_error, load_object

import soml
from soml._parser import parse as parse_document

CONFORMANCE = Path(__file__).parent / 'conformance' / 'valid'

INSTANT_FORMAT = 'An instant is written as 2026-09-19T14:00:00Z, with an optional fraction of up to nine digits and an offset of Z or ±HH:MM'
COMMENT_HINT = '“#” starts a comment, so a value that starts with “#” must be quoted'
SEPARATOR = 'The date and time separator in an instant is an uppercase “T”, not a space'
OCTAL_REASON = "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o{digits}, and an identifier, such as a ZIP code, as a string: '{text}'"
IDENTIFIER_REASON = "Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '{text}'"
THOUSANDS_REASON = 'Leading zeros are not allowed in a decimal number. A comma separates items, so {number} is not one number'
UNIT_REASON = "A unit cannot follow a number after a space. Write a duration without the space, as in {duration}, and anything else, such as a size, as a string, as in '{text}'"
TABLE_HEADER_REASON = 'There are no table headers. Write the table as an object, as in {name}: {{…}}'
DOT_IN_KEY_REASON = 'A key cannot contain “.” unless it is quoted. Quote the whole key, or use braces to nest, as in a: {b: …}'

BIDIRECTIONAL_CONTROLS = ['\u061c', '\u200e', '\u200f', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069']

# The character set with the controls that a terminal acts on: the C1 control CSI and every bidirectional control.
TERMINAL_CONTROLS = ['\x9b', *BIDIRECTIONAL_CONTROLS]


def is_valid(text: str) -> bool:
	"""Whether `text` is a valid document, as the spec says, which includes a value finer than a microsecond that `loads()` cannot hold."""
	try:
		parse_document(text, truncates_nanoseconds=True)
	except soml.SOMLDecodeError:
		return False

	return True


# The error type


def test_a_decode_error_is_a_value_error() -> None:
	error = decode_error('')
	assert isinstance(error, ValueError)


def test_a_decode_error_has_the_reason_and_the_position() -> None:
	error = decode_error('a: 1\nb: nope\n')
	assert error.msg == "Unexpected “nope”. A string value must be quoted, as in 'nope'"
	assert error.doc == 'a: 1\nb: nope\n'
	assert error.pos == 8
	assert error.lineno == 2
	assert error.colno == 4


def test_the_message_is_the_reason_and_the_position() -> None:
	error = decode_error('a: 1\nb: nope\n')
	assert str(error) == f'{error.msg} (at line 2, column 4)'
	assert error.args == (str(error),)


def test_the_document_of_bytes_is_the_decoded_text() -> None:
	error = decode_error('a: 1\nb: nopé'.encode())
	assert error.doc == 'a: 1\nb: nopé'
	assert error.doc[error.pos :].startswith('nopé')


def test_the_document_of_invalid_utf8_is_the_text_before_the_bad_byte() -> None:
	error = decode_error(b'a: 1\nb: \xff\nc: 3')
	assert error.doc == 'a: 1\nb: '
	assert error.pos == len(error.doc)


def test_a_decode_error_survives_pickling() -> None:
	error = decode_error('a: 1\nb: nope\n')
	restored = pickle.loads(pickle.dumps(error))  # noqa: S301 # The data was pickled right here.
	assert type(restored) is soml.SOMLDecodeError
	assert str(restored) == str(error)
	assert (restored.msg, restored.doc, restored.pos, restored.lineno, restored.colno) == (error.msg, error.doc, error.pos, error.lineno, error.colno)


def test_a_decode_error_survives_copying() -> None:
	error = decode_error('a: nope')
	assert str(copy.copy(error)) == str(error)
	assert copy.deepcopy(error).colno == error.colno


def test_a_decode_error_can_be_constructed_like_json_decode_error() -> None:
	error = soml.SOMLDecodeError('Bad', 'a: 1\nbc', 6)
	assert (error.lineno, error.colno) == (2, 2)
	assert str(error) == 'Bad (at line 2, column 2)'


def test_invalid_utf8_errors_suppress_the_unicode_decode_error_context() -> None:
	error = decode_error(b'\xff')
	assert error.__suppress_context__


@pytest.mark.parametrize('character', TERMINAL_CONTROLS, ids=[f'U+{ord(character):04X}' for character in TERMINAL_CONTROLS])
def test_terminal_and_bidirectional_controls_in_duplicate_keys_are_sanitized(character: str) -> None:
	key = f'\\u{{{ord(character):x}}}admin'
	error = decode_error(f'"{key}": 1\n"{key}": 2')
	assert error.msg == 'Duplicate key "\ufffdadmin"'
	assert str(error) == f'{error.msg} (at line 2, column 1)'
	assert character not in str(error)


@pytest.mark.parametrize('character', TERMINAL_CONTROLS, ids=[f'U+{ord(character):04X}' for character in TERMINAL_CONTROLS])
def test_terminal_and_bidirectional_controls_in_unknown_escapes_are_sanitized(character: str) -> None:
	error = decode_error(f'a: "\\{character}"')
	assert error.msg.startswith('Unknown escape “\\\ufffd”')
	assert str(error) == f'{error.msg} (at line 1, column 5)'
	assert character not in str(error)


@pytest.mark.parametrize('character', BIDIRECTIONAL_CONTROLS, ids=[f'U+{ord(character):04X}' for character in BIDIRECTIONAL_CONTROLS])
def test_bidirectional_controls_in_quoted_keys_are_sanitized(character: str) -> None:
	error = decode_error(f"'{character}admin': 1\n'{character}admin': 2")
	assert character not in str(error)
	assert (error.lineno, error.colno) == (2, 1)


def test_error_messages_quote_at_most_40_characters_of_a_token() -> None:
	error = decode_error(f'a: {"x" * 1_000_000}')
	assert len(str(error)) < 200


def test_a_quoted_token_is_shortened_by_utf16_code_units_as_in_js() -> None:
	key = '😀' * 30
	assert decode_error(f'"{key}": 1\n"{key}": 2').msg == f'Duplicate key "{"😀" * 20}…"'

	# A surrogate pair that would be split at the limit is left out whole.
	key = f'a{"😀" * 30}'
	assert decode_error(f'"{key}": 1\n"{key}": 2').msg == f'Duplicate key "a{"😀" * 19}…"'


def test_the_key_quoting_hint_shows_no_example_for_a_key_of_1000_characters() -> None:
	assert decode_error(f'${"a" * 998}: 1').msg.endswith(f", as in '${'a' * 39}…'")
	assert decode_error(f'${"a" * 999}: 1').msg.endswith('must be quoted')
	# The length is in UTF-16 code units, as in JS.
	assert decode_error(f'${"😀" * 499}: 1').msg.endswith("…'")
	assert decode_error(f'${"😀" * 500}: 1').msg.endswith('must be quoted')


def test_the_size_hint_is_not_given_for_a_number_of_more_than_1000_characters() -> None:
	assert decode_error(f'a: {"1" * 1001}M').msg.endswith('there is no month unit, because a month is not a fixed length')


def test_a_raw_control_character_is_reported_before_an_earlier_lone_surrogate() -> None:
	error = decode_error("a: '\ud800'\nb: '\x01'")
	assert error.msg.startswith('A raw control character (U+0001)')
	assert (error.lineno, error.colno) == (2, 5)


def test_a_huge_number_gives_a_short_message() -> None:
	error = decode_error(f'a: {"9" * 100_000}')
	assert len(str(error)) < 200


# Positions


def test_an_empty_document_points_at_the_start() -> None:
	error = decode_error('')
	assert (error.lineno, error.colno, error.pos) == (1, 1, 0)


def test_line_and_column_are_1_based() -> None:
	error = decode_error('}')
	assert (error.lineno, error.colno, error.pos) == (1, 1, 0)


def test_a_column_counts_code_points_so_an_emoji_is_one_column() -> None:
	error = decode_error("a: '😀😀' x")
	assert (error.colno, error.pos) == (9, 8)


def test_a_column_counts_code_points_not_bytes() -> None:
	error = decode_error("'日本語': 01")
	assert (error.colno, error.pos) == (8, 7)
	assert decode_error("'日本語': 01".encode()).colno == 8


@pytest.mark.parametrize('character', ['\U00010000', '\U0010ffff'])
def test_the_column_after_astral_characters(character: str) -> None:
	assert decode_error(f"a: '{character * 3}' nope").colno == 10


def test_a_tab_is_one_column() -> None:
	assert decode_error('\t\ta: nope').colno == 6


def test_an_error_on_a_later_line() -> None:
	error = decode_error('a: 1\n\n\n# c\nb: 2\nc: nope')
	assert (error.lineno, error.colno) == (6, 4)


def test_an_error_at_the_end_of_the_document_points_past_the_last_character() -> None:
	error = decode_error('a:')
	assert error.msg == 'Expected a value, but reached the end of the document'
	assert (error.colno, error.pos) == (3, 2)


def test_an_unterminated_container_points_at_its_opening() -> None:
	error = decode_error('a: [1, 2')
	assert error.msg == 'Unterminated array: expected “]”'
	assert (error.lineno, error.colno) == (1, 4)


def test_an_error_right_after_a_line_break_is_on_the_next_line() -> None:
	error = decode_error('a: 1\n}')
	assert (error.lineno, error.colno) == (2, 1)


def test_an_error_on_a_line_break_character_is_on_the_line_it_ends() -> None:
	error = decode_error('{a\n: 1}')
	assert (error.pos, error.lineno, error.colno) == (2, 1, 3)


def test_an_unterminated_string_points_at_its_opening_quote() -> None:
	error = decode_error("a: 'x\n")
	assert (error.lineno, error.colno) == (1, 4)
	error = decode_error("a: 1\nb: 'never closed\nc: 3")
	assert (error.lineno, error.colno) == (2, 4)


@pytest.mark.parametrize(
	('source', 'position'),
	[
		('a.b: 1', (1, 2)),
		('example.com: 1', (1, 8)),
		("3.14: 'x'", (1, 2)),
		("'a'.b: 1", (1, 4)),
		("a.'b': 1", (1, 2)),
		('{a.b: 1}', (1, 3)),
		('x: {a.b: 1}', (1, 6)),
		('[{a.b: 1}]', (1, 4)),
		('a: 1\n\tb.c: 2', (2, 3)),
		('.a: 1', (1, 1)),
	],
)
def test_a_dot_in_a_key_points_at_the_dot(source: str, position: tuple[int, int]) -> None:
	error = decode_error(source)
	assert (error.lineno, error.colno) == position


def test_the_position_of_a_duplicate_key_is_the_second_key() -> None:
	error = decode_error('{\n\ta: 1,\n\ta: 2,\n}')
	assert (error.lineno, error.colno) == (3, 2)
	error = decode_error('a: 1\nb: 2\n  a: 3')
	assert (error.lineno, error.colno) == (3, 3)


def test_an_unterminated_block_comment_points_at_its_opening() -> None:
	error = decode_error('a: 1\n\n  /* x\n\n')
	assert (error.lineno, error.colno) == (3, 3)


def test_a_nested_block_comment_points_at_the_inner_opening() -> None:
	error = decode_error('a: 1\n/* x /* y */')
	assert (error.lineno, error.colno) == (2, 6)


def test_a_position_after_astral_characters_on_earlier_lines() -> None:
	error = decode_error("a: '😀😀😀'\nb: nope")
	assert (error.lineno, error.colno) == (2, 4)


def test_an_escape_error_points_at_the_backslash() -> None:
	error = decode_error('a: "abc\\qdef"')
	assert (error.lineno, error.colno) == (1, 8)


def test_an_escape_error_in_a_block_string_points_at_the_escape() -> None:
	error = decode_error('a: """\n\tok\n\t  x\\q\n\t"""')
	assert error.msg.startswith('Unknown escape “\\q”')
	assert (error.lineno, error.colno) == (3, 5)


def test_a_content_line_indented_less_than_the_closing_delimiter_points_at_the_line() -> None:
	error = decode_error("a: '''\n\tx\n y\n\t'''")
	assert "indentation of its block string's closing delimiter" in error.msg
	assert (error.lineno, error.colno) == (3, 1)


def test_an_unterminated_block_string_points_at_its_opening() -> None:
	error = decode_error("a: 1\nb: '''\n\tone\n\ttwo\n")
	assert error.msg == 'Unterminated block string'
	assert (error.lineno, error.colno) == (2, 4)


def test_a_long_line_with_an_error_at_the_end_reports_the_right_column() -> None:
	error = decode_error(f"a: '{'x' * 100_000}' 1")
	assert (error.lineno, error.colno) == (1, 100_007)


def test_many_lines_with_an_error_at_the_end_reports_the_right_line() -> None:
	source = 'a: 1\n' + ''.join(f'k{index}: 1\n' for index in range(1, 100_000)) + 'bad'
	assert decode_error(source).lineno == 100_001


@pytest.mark.parametrize(
	('source', 'line', 'column'),
	[
		('a: 1\r\nb: 2', 1, 5),
		('a: 1\nb: 2\r', 2, 5),
		('\r', 1, 1),
		("a: '\r'", 1, 5),
		('# comment \r\na: 1', 1, 11),
		('/* \r */ a: 1', 1, 4),
		("a: '''\n\tx\r\n\t'''", 2, 3),
	],
)
def test_a_carriage_return_is_rejected_anywhere_with_its_position(source: str, line: int, column: int) -> None:
	error = decode_error(source)
	assert error.msg == 'A carriage return (U+000D) is not allowed anywhere. Use LF line endings'
	assert (error.lineno, error.colno) == (line, column)


CONTROL_CHARACTERS = [*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F]

CONTROL_CHARACTER_CONTEXTS = [
	("a: 'x", "y'"),
	('a: "x', 'y"'),
	("a: '''\n\tx", "\n\t'''"),
	('a: """\n\tx', '\n\t"""'),
	("'x", "y': 1"),
	('"x', 'y": 1'),
	('# x', '\na: 1'),
	('/* x', ' */\na: 1'),
	('a:', '1'),
	('a: 1', '\nb: 2'),
	('[1,', '2]'),
]


@pytest.mark.parametrize('code_point', CONTROL_CHARACTERS, ids=[f'U+{code_point:04X}' for code_point in CONTROL_CHARACTERS])
def test_a_raw_control_character_is_an_error_even_in_a_string_or_a_comment(code_point: int) -> None:
	for prefix, suffix in CONTROL_CHARACTER_CONTEXTS:
		error = decode_error(f'{prefix}{chr(code_point)}{suffix}'.encode())
		assert (
			error.msg
			== f'A raw control character (U+{code_point:04X}) is not allowed anywhere, including in strings and comments. In a string, write it as the escape \\u{{{code_point:x}}} inside "..."'
		)
		assert error.pos == len(prefix)


INVISIBLE_CHARACTERS = ['\u00a0', '\u2000', '\u2003', '\u2009', '\u200b', '\u2028', '\u2029', '\u3000', '\x85', '\ufeff', '\u2060']


@pytest.mark.parametrize('character', INVISIBLE_CHARACTERS, ids=[f'U+{ord(character):04X}' for character in INVISIBLE_CHARACTERS])
def test_an_invisible_character_outside_a_string_is_an_error_that_names_it(character: str) -> None:
	code = f'U+{ord(character):04X}'

	# The same character at the start of the document is a BOM when it is U+FEFF, which has its own error.
	sources = [f'a:{character}1', f'a: 1{character}', f'[1,{character}2]']

	if character != '\ufeff':
		sources.append(f'{character}a: 1')

	for source in sources:
		assert code in decode_error(source).msg, source


def test_a_bom_later_in_the_document_is_not_whitespace() -> None:
	assert 'U+FEFF' in decode_error('a: 1\n\ufeffb: 2').msg
	assert 'U+FEFF' in decode_error('a:\ufeff1').msg


def test_a_bom_inside_a_string_is_content() -> None:
	assert soml.loads("a: '\ufeff'") == {'a': '\ufeff'}


# Reasons, from soml-js


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('', 'A document must contain an object or an array, but this one is empty'),
		('# only a comment', 'A document must contain an object or an array, but this one is empty'),
		('5', 'A bare value is not a document. A document is an object or an array, so write it as `key: value` or `[value]`'),
		('2026-09-19T14:00:00Z', 'A bare value is not a document. A document is an object or an array, so write it as `key: value` or `[value]`'),
		("'hello'", 'A bare value is not a document. A document is an object or an array, so write it as `key: value` or `[value]`'),
		('a: 1, b: 2', 'Top-level entries are separated by line breaks, not commas. Use braces for a one-line object'),
		('a: 1\n, b: 2', 'Top-level entries are separated by line breaks, not commas. Use braces for a one-line object'),
		('a: 1 b: 2', 'Expected a line break before the next entry, but found “b”'),
		('a: 512 MiB', "A unit cannot follow a number after a space. Write a duration without the space, as in 10s, and anything else, such as a size, as a string, as in '512 MiB'"),
		('a: 10 seconds', "A unit cannot follow a number after a space. Write a duration without the space, as in 10s, and anything else, such as a size, as a string, as in '10 seconds'"),
		('a: 100 ms', "A unit cannot follow a number after a space. Write a duration without the space, as in 100ms, and anything else, such as a size, as a string, as in '100 ms'"),
		# `m` may be meters, so the string is offered too.
		('a: 10 m', "A unit cannot follow a number after a space. Write a duration without the space, as in 10m, and anything else, such as a size, as a string, as in '10 m'"),
		('a: 1.5 h # note', "A unit cannot follow a number after a space. Write a duration without the space, as in 1.5h, and anything else, such as a size, as a string, as in '1.5 h'"),
		('a: [100 ms]', "A unit cannot follow a number after a space. Write a duration without the space, as in 100ms, and anything else, such as a size, as a string, as in '100 ms'"),
		('a: {b: 100 ms}', "A unit cannot follow a number after a space. Write a duration without the space, as in 100ms, and anything else, such as a size, as a string, as in '100 ms'"),
		('a: [1 true]', 'Expected “,”, a line break, or “]” after an array item, but found “t”'),
		('a: 5 and more', 'Expected a line break before the next entry, but found “a”'),
		("a: 'x' MiB", 'Expected a line break before the next entry, but found “M”'),
		('// note\na: 1', 'Expected a key, but found “/”. A comment starts with “#”'),
		('a: 1 // note', 'Expected a line break before the next entry, but found “/”. A comment starts with “#”'),
		('a: [1 // note\n]', 'Expected “,”, a line break, or “]” after an array item, but found “/”. A comment starts with “#”'),
		('a: [1, // note\n2]', 'Expected a value, but found “/”. A comment starts with “#”'),
		('a: 1 / 2', 'Expected a line break before the next entry, but found “/”'),
		('key: |\n  text', "Expected a value, but found “|”. Write a multiline string as a block string, between ''' lines"),
		# A folded block scalar joins its lines, which a block string does not.
		('key: >-\n  text', 'Expected a value, but found “>”'),
		('key: | x', 'Expected a value, but found “|”'),
		('[server]\nport: 1', 'There are no table headers. Write the table as an object, as in server: {…}'),
		('[[servers]]\nport: 1', 'There are no table headers. Write the table as an object, as in servers: {…}'),
		('a: [server]', "Unexpected “server”. A string value must be quoted, as in 'server'"),
		("name: 'x'\n\n[server]\nport: 1", 'There are no table headers. Write the table as an object, as in server: {…}'),
		('a: 1\n[', 'Expected a key, but found “[”'),
		('{2026-09-19T14:00:00Z: 1}', "A key that contains “:” must be quoted, as in '2026-09-19T14:00:00Z'"),
		("12:30: 'lunch'", "A key that contains “:” must be quoted, as in '12:30'"),
		('a:b: 1', "A key that contains “:” must be quoted, as in 'a:b'"),
		('a:b', "Unexpected “b”. A string value must be quoted, as in 'b'"),
		('a:{b: nope}', "Unexpected “nope”. A string value must be quoted, as in 'nope'"),
		('{a: 1} x', 'Unexpected “x” after the end of the document'),
		('the name: 1', "A bare key cannot contain spaces. Quote it, as in 'the name'"),
		('a : 1', 'Whitespace is not allowed between a key and its “:”'),
		('a = 1', 'Expected “:” after the key, but found “=”'),
		('a.b c: 1', DOT_IN_KEY_REASON),
		("'a' b: 1", 'Expected “:” after the key, but found “b”'),
		('a$: 1', 'Expected “:” after the key, but found “$”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted'),
		('a:\nb: 1', 'Expected a value, but found the key b'),
		("a:\n'b c': 1", 'Expected a value, but found the key "b c"'),
		('a:\n007: 1', 'Expected a value, but found the key 007'),
		('a:\n2026-09-19T25:00:00Z', 'Invalid instant 2026-09-19T25:00:00Z: the hour must be 00 to 23'),
		('a:\n\t12:30', 'A time of day is a string, so it must be quoted'),
		('a: 07:32:00', 'A time of day is a string, so it must be quoted'),
		('a: 12:00', 'A time of day is a string, so it must be quoted'),
		('a: 10:30 PM', 'A time of day is a string, so it must be quoted'),
		('a: 8080:80', "Invalid number “8080:80”. A value that contains “:” must be quoted, as in '8080:80'"),
		('color: #FFF\nsize: 1', f"Expected a value, but found the key size. {COMMENT_HINT}, as in '#FFF'"),
		('{color: #FFF\nsize: 1}', f"Expected a value, but found the key size. {COMMENT_HINT}, as in '#FFF'"),
		('color: #FFF', f"Expected a value, but reached the end of the document. {COMMENT_HINT}, as in '#FFF'"),
		('color: #FFF\n', f"Expected a value, but reached the end of the document. {COMMENT_HINT}, as in '#FFF'"),
		('{color: #FFF\n}', f"Expected a value, but found “}}”. {COMMENT_HINT}, as in '#FFF'"),
		('color: #FFF, size: 1', f"Expected a value, but reached the end of the document. {COMMENT_HINT}, as in '#FFF'"),
		('color: # The color\nsize: 1', 'Expected a value, but found the key size'),
		('server:\n  port: 80', 'Expected a value, but found the key port. Indentation does not nest objects, so write server: {port: …}'),
		('ports:\n  - 80\n  - 443', 'Expected a value, but found a “-” list. An array is written in brackets, as in [80, 443]'),
		('ports:\n- 80', 'Expected a value, but found a “-” list. An array is written in brackets, as in [80, 443]'),
		('{\n\ta:\n\tb: 1\n}', 'Expected a value, but found the key b'),
		("a:\n'b':x", 'Expected a value, but found the key b'),
		("a:\n'b.c': 1", 'Expected a value, but found the key "b.c"'),
		# The octal suggestion is only for the number on its own.
		('a:\n007 1', "Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '007 1'"),
		('[1 /*\n*/ 2]', 'Expected “,”, a line break, or “]” after an array item, but found “2”. A line break inside a block comment does not separate items'),
		('{a: 1 /*\n*/ b: 2}', 'Expected “,”, a line break, or “}” after an object member, but found “b”. A line break inside a block comment does not separate items'),
		('[1 /* c */ 2]', 'Expected “,”, a line break, or “]” after an array item, but found “2”'),
		('a: 1\na: 2', 'Duplicate key a'),
		('a: hello', "Unexpected “hello”. A string value must be quoted, as in 'hello'"),
		('a: yes', '“yes” is not a value. Booleans are written true and false, in lowercase'),
		('a: True', '“True” is not a value. Booleans are written true and false, in lowercase'),
		('a: NULL', '“NULL” is not a value. Null is written null, in lowercase'),
		('a: nan', 'NaN is not representable. Use null for a missing value'),
		('a: inf', '“inf” is not a value. Infinity is written infinity, in lowercase'),
		('a: -inf', '“-inf” is not a value. Negative infinity is written -infinity'),
		('a: +1', 'A “+” sign is not allowed. A number without a sign is positive'),
		('a: 1e+5', 'A “+” sign is not allowed in a number, including in an exponent'),
		('a: .5', 'A number cannot begin with “.”; write a digit before it, as in 0.5'),
		('a: 5.', 'A decimal point must be followed by a digit'),
		('a: 01', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o1, and an identifier, such as a ZIP code, as a string: '01'"),
		('mode: 0644', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o644, and an identifier, such as a ZIP code, as a string: '0644'"),
		('zip: 08901', "Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '08901'"),
		# The digit that is not octal comes after the length that is diagnosed, so the whole number decides that there is no octal suggestion.
		(f'a: 0{"0" * 1100}8', f"Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '{'0' * 40}…'"),
		(f'a: 0{"0" * 1100}7', f"Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '{'0' * 40}…'"),
		('a: 01.5', 'Leading zeros are not allowed in a decimal number'),
		# An octal number that long is outside the 64-bit range, so it is not suggested.
		(f'a: 0{"7" * 50}', f"Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '0{'7' * 39}…'"),
		('a: 1e05', 'Leading zeros are not allowed in an exponent'),
		('a: 1e-0', '“e-0” is not allowed, because an exponent of zero has one spelling: e0'),
		('a: -0', '“-0” is not allowed, because zero has one spelling: 0'),
		('a: 1__0', 'An underscore in a number must be between two digits'),
		('a: 1E5', 'An exponent marker is a lowercase “e”'),
		('a: 0xff', 'Hexadecimal digits are uppercase: 0xFF'),
		('a: 0XFF', 'A number prefix is lowercase: 0x, 0o, or 0b'),
		('a: -0xFF', 'A hexadecimal integer cannot have a sign, because it states a bit pattern rather than a quantity'),
		('a: 0o9', 'Invalid octal digit “9”'),
		('a: 0b2', 'Invalid binary digit “2”'),
		('a: 0x', 'Expected hexadecimal digits after “0x”'),
		('a: 1.2.3', 'Invalid number “1.2.3”. A value with several dots, such as a version number, must be quoted'),
		('a: 10sec', 'Invalid duration 10sec: “sec” is not a unit. The units are h, m, s, ms, us, and ns, and a string must be quoted'),
		('a: 2days', 'Invalid duration 2days: there is no day unit, because a day is not a fixed length. Write 24h for a fixed 24 hours'),
		('a: 12abc', "Invalid number “12abc”. A string value must be quoted, as in '12abc'"),
		('a: 1d', 'Invalid duration 1d: there is no day unit, because a day is not a fixed length. Write 24h for a fixed 24 hours'),
		('a: 1H', 'Invalid duration 1H: the units are lowercase: h'),
		('retention: 6M', "Invalid duration 6M: there is no month unit, because a month is not a fixed length. A size, such as 512M, is a string: '6M'"),
		('memory: 512M', "Invalid duration 512M: there is no month unit, because a month is not a fixed length. A size, such as 512M, is a string: '512M'"),
		('memory: 1_024M', "Invalid duration 1_024M: there is no month unit, because a month is not a fixed length. A size, such as 512M, is a string: '1_024M'"),
		('a: 1h6M', 'Invalid duration 1h6M: there is no month unit, because a month is not a fixed length'),
		('a: 1.5M', 'Invalid duration 1.5M: there is no month unit, because a month is not a fixed length'),
		('a: 1.5h30m', 'Invalid duration 1.5h30m: only the last part may have a fraction'),
		('a: 0.5ns', 'Invalid duration 0.5ns: it is not a whole number of nanoseconds'),
		('a: 1.s', 'Invalid duration 1.s: a “.” must be followed by a digit'),
		('a: 2562048h30x', 'Invalid duration 2562048h30x: “x” is not a unit. The units are h, m, s, ms, us, and ns, and a string must be quoted'),
		('a: 1.5s-', 'Invalid duration 1.5s-: expected a number at “-”'),
		('a: 1h.5m', 'Invalid duration 1h.5m: expected a number at “.5m”'),
		('a: 1.5d', 'Invalid duration 1.5d: there is no day unit, because a day is not a fixed length. Write 24h for a fixed 24 hours'),
		('a: 2weeks', 'Invalid duration 2weeks: there is no week unit, because a day is not a fixed length. Write 168h for a fixed 168 hours'),
		('a: 0_5s', 'Invalid duration 0_5s: leading zeros are not allowed'),
		('a: 1.5_s', 'Invalid duration 1.5_s: an underscore must be between two digits'),
		('a: 30m1h', 'Invalid duration 30m1h: the units are in the order h, m, s, ms, us, ns, and each appears at most once'),
		('a: 1h05m', 'Invalid duration 1h05m: leading zeros are not allowed'),
		('a: 1_s', 'Invalid duration 1_s: an underscore must be between two digits'),
		('a: 1h-30m', 'Invalid duration 1h-30m: only the whole duration takes a sign, as in -1h30m'),
		('a: 1h+30m', 'Invalid duration 1h+30m: a “+” sign is not allowed'),
		('a: 1h30', 'Invalid duration 1h30: every number needs a unit: h, m, s, ms, us, or ns'),
		('a: -0s', 'Invalid duration -0s: “-” is not allowed before zero, because zero has one spelling: 0s'),
		('a: 2562048h', 'Invalid duration 2562048h: it is outside the 64-bit range of nanoseconds, about 292 years either way'),
		('a: 1.5x', "Invalid number “1.5x”. A string value must be quoted, as in '1.5x'"),
		('a: 1e3s', "Invalid number “1e3s”. A string value must be quoted, as in '1e3s'"),
		('a: 9223372036854775808', 'The integer 9223372036854775808 is outside the 64-bit range (-9223372036854775808 to 9223372036854775807)'),
		('a: 1e999', '1e999 is too large to be a finite float. Use infinity if you mean it'),
		('a: -1e999', '-1e999 is too large to be a finite float. Use -infinity if you mean it'),
		('a: 1e-400', '1e-400 is too small to be told apart from zero. Write 0.0 if you mean zero'),
		('a: -1e-400', '-1e-400 is too small to be told apart from zero. Write 0.0 if you mean zero'),
		('a: 2026-09-19t14:00:00Z', 'The date and time separator in an instant is an uppercase “T”'),
		('a: 2026-09-19T14:00:00z', 'The UTC offset in an instant is an uppercase “Z”'),
		(
			'a: 2026-09-19T14:00:00',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00.5',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00.5', or add the offset it was meant in",
		),
		('a: 1979-05-27 07:32:00Z', 'The date and time separator in an instant is an uppercase “T”, not a space, as in 1979-05-27T07:32:00Z'),
		('a: 1979-05-27 07:32:00.5+02:00', 'The date and time separator in an instant is an uppercase “T”, not a space, as in 1979-05-27T07:32:00.5+02:00'),
		('a: [1979-05-27 07:32:00Z]', 'The date and time separator in an instant is an uppercase “T”, not a space, as in 1979-05-27T07:32:00Z'),
		(
			'a: 1979-05-27 07:32:00',
			"The date and time separator in an instant is an uppercase “T”, not a space, and an instant needs the offset it was meant in, as in 1979-05-27T07:32:00Z for UTC. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '1979-05-27 07:32:00'",
		),
		('a: 1979-05-27 07:32:00,5Z', f'The date and time separator in an instant is an uppercase “T”, not a space. {INSTANT_FORMAT}'),
		('a: 1979-05-27 07:32', f'The date and time separator in an instant is an uppercase “T”, not a space. {INSTANT_FORMAT}'),
		('a: 2026-09-19T14:00:00+0700', "An instant's offset is written with a colon, as in +07:00"),
		('a: 2026-02-30T00:00:00Z', 'Invalid instant 2026-02-30T00:00:00Z: the day must be 01 to 28 in that month'),
		('a: 2026-12-31T23:59:60Z', 'Invalid instant 2026-12-31T23:59:60Z: the second must be 00 to 59, and a leap second is not representable'),
		("a: 'x", "Unterminated string. A '...' string must end on the line it starts on; use a block string (''') for multiple lines"),
		('a: "x', 'Unterminated string. A "..." string must end on the line it starts on; use a block string (""") for multiple lines'),
		(r'a: "\q"', r"""Unknown escape “\q”. The escapes are \\, \", \n, \t, and \u{…}; use a '...' string for literal backslashes"""),
		(r'a: "\r"', r'There is no \r escape, because a carriage return cannot be represented'),
		(r'a: "\'"', 'A \' needs no escape inside "..."'),
		(r'a: "\u0041"', r'The four-digit \u0041 form is not an escape. Write \u{41}'),
		(r'a: "\u{0041}"', r'A Unicode escape may not have leading zeros; write \u{41}'),
		(r'a: "\u{E9}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{d800}"', r'\u{d800} is a surrogate, which is not a Unicode scalar value'),
		(r'a: "\u{110000}"', r'\u{110000} is above U+10FFFF, the largest Unicode scalar value'),
		(r'a: "\u{d}"', r'A carriage return (U+000D) cannot be represented, so \u{d} is not allowed'),
		("a: '''x\n'''", "A block string's opening delimiter must be followed directly by a line break, and its content starts on the next line"),
		("a: '''\nx", 'Unterminated block string'),
		("a: '''\nWelcome!'''", "Unterminated block string. Its closing delimiter must start a line, so move the ''' at the end of line 2 to a new line"),
		('a: """\n\tHello\n\tWelcome!"""  \nb: 1', 'Unterminated block string. Its closing delimiter must start a line, so move the """ at the end of line 3 to a new line'),
		('a: [1,000]', 'Leading zeros are not allowed in a decimal number. A comma separates items, so 1,000 is not one number. Write it as 1_000 or 1000'),
		('a: [-1,000]', 'Leading zeros are not allowed in a decimal number. A comma separates items, so -1,000 is not one number. Write it as -1_000 or -1000'),
		('a: [12,345,000]', 'Leading zeros are not allowed in a decimal number. A comma separates items, so 12,345,000 is not one number. Write it as 12_345_000 or 12345000'),
		('a: [1,234,567,000]', 'Leading zeros are not allowed in a decimal number. A comma separates items, so 1,234,567,000 is not one number. Write it as 1_234_567_000 or 1234567000'),
		('a: [12,050,000]', 'Leading zeros are not allowed in a decimal number. A comma separates items, so 12,050,000 is not one number. Write it as 12_050_000 or 12050000'),
		('a: [0x1,000]', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o0, and an identifier, such as a ZIP code, as a string: '000'"),
		('a: [1, 000]', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o0, and an identifier, such as a ZIP code, as a string: '000'"),
		('a: 1.5µs', 'The unit for microseconds is written us, as in 1.5us'),
		('a: 3μs', 'The unit for microseconds is written us, as in 3us'),
		("a: 'it''s'", "There is no '' escape in a '...' string. Write a string that contains ' as \"...\", as in \"it's\""),
		(
			"a: '''\n\tx\n\t\t'''",
			"This line does not start with the indentation of its block string's closing delimiter. Every line except a blank one must start with exactly the same spaces and tabs",
		),
		("'''\nk\n''': 1", 'A block string cannot be a key'),
		('a: 1\n/* x', 'Unterminated block comment'),
		('/* a /* b */\na: 1', 'Block comments cannot be nested, and their body may not contain “/*”'),
		('a: [1 2]', 'Expected “,”, a line break, or “]” after an array item, but found “2”'),
		('a: {b: 1 c: 2}', 'Expected “,”, a line break, or “}” after an object member, but found “c”'),
		('a: [1', 'Unterminated array: expected “]”'),
		('a: {b: 1', 'Unterminated object: expected “}”'),
		('a:\u00a01', 'Expected a value, but found U+00A0 (NO-BREAK SPACE; only space, tab, and line feed are whitespace)'),
		('a: 1\u2028', 'Unexpected U+2028 (LINE SEPARATOR; only space, tab, and line feed are whitespace) after a value'),
		('a: 1\r\n', 'A carriage return (U+000D) is not allowed anywhere. Use LF line endings'),
		('\ufeffa: 1', 'A byte order mark (BOM) is not allowed'),
		('a: \x01', 'A raw control character (U+0001) is not allowed anywhere, including in strings and comments. In a string, write it as the escape \\u{1} inside "..."'),
		('a: `x`', 'Expected a value, but found “`”'),
		('a: \u202ex', 'Expected a value, but found U+202E (RIGHT-TO-LEFT OVERRIDE)'),
		('a: \ue000', 'Expected a value, but found U+E000'),
		('a: \ufff9', 'Expected a value, but found U+FFF9'),
		('a: 😀', 'Expected a value, but found “😀”'),
		('a.b: 1', "A bare key cannot contain “.”. Quote it, as in 'a.b', or use braces to nest, as in a: {b: …}"),
		('example.com: 1', "A bare key cannot contain “.”. Quote it, as in 'example.com', or use braces to nest, as in example: {com: …}"),
		('a.b.c: 1', "A bare key cannot contain “.”. Quote it, as in 'a.b.c', or use braces to nest, as in a: {b: {c: …}}"),
		# A number is a valid bare key, so `3.14` as a key is a key with a dot, not a float.
		("3.14: 'x'", "A bare key cannot contain “.”. Quote it, as in '3.14', or use braces to nest, as in 3: {14: …}"),
		('1.5s: 1', "A bare key cannot contain “.”. Quote it, as in '1.5s', or use braces to nest, as in 1: {5s: …}"),
		('{a.b: 1}', "A bare key cannot contain “.”. Quote it, as in 'a.b', or use braces to nest, as in a: {b: …}"),
		# The suggestions are only given when both are valid: a bare key with a word on both sides of every dot, and a `:` right after it.
		("'a'.b: 1", DOT_IN_KEY_REASON),
		("a.'b': 1", DOT_IN_KEY_REASON),
		('a.: 1', DOT_IN_KEY_REASON),
		('a..b: 1', DOT_IN_KEY_REASON),
		('a.b', DOT_IN_KEY_REASON),
		('.a: 1', "Expected a key, but found “.”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '.a'"),
		('.env: 1', "Expected a key, but found “.”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '.env'"),
		('files: {.eslintrc.json: 1}', "Expected a key, but found “.”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '.eslintrc.json'"),
		("'a.b': 1\n'a.b': 2", 'Duplicate key "a.b"'),
		(f'a: {"[" * 100}{"]" * 100}', 'The document is nested more than 100 levels deep'),
	],
)
def test_a_rejected_document_reports_its_reason(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 0xff_ff', 'Hexadecimal digits are uppercase: 0xFF_FF'),
		('a: 0xFf', 'Hexadecimal digits are uppercase: 0xFF'),
		('a: 0x00ff', 'Hexadecimal digits are uppercase: 0x00FF'),
		('a: 0x7fffffffffffffff', 'Hexadecimal digits are uppercase: 0x7FFFFFFFFFFFFFFF'),
		('a: 0x0000_7fff_ffff_ffff_ffff', 'Hexadecimal digits are uppercase: 0x0000_7FFF_FFFF_FFFF_FFFF'),
		# The uppercase spelling would be invalid too, so the underscore is reported first.
		('a: 0x_ff', 'An underscore in a number must be between two digits'),
		('a: 0xff_', 'An underscore in a number must be between two digits'),
		('a: 0xf__f', 'An underscore in a number must be between two digits'),
		('a: 0x_FF', 'An underscore in a number must be between two digits'),
		# The uppercase spelling would be outside the 64-bit range, so there is no example.
		('a: 0x8000000000000000a', 'Hexadecimal digits are uppercase'),
		('a: 0xffffffffffffffff', 'Hexadecimal digits are uppercase'),
		# The lowercase digits come after the length that is diagnosed, so the whole number decides the reason.
		(f'a: 0x{"1" * 1100}ff', 'Hexadecimal digits are uppercase'),
		(f'a: 0x{"1" * 1100}f_f', 'Hexadecimal digits are uppercase'),
		(f'a: 0x{"1" * 1100}f__f', 'An underscore in a number must be between two digits'),
	],
)
def test_a_suggested_uppercase_hexadecimal_integer_is_valid_and_in_range(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('digits', ['a', 'f', '0a', 'a0', 'ab_cd', 'a_b_c', '7fffffffffffffff', 'dead_beef'])
def test_a_suggested_uppercase_hexadecimal_integer_means_what_the_lowercase_one_does(digits: str) -> None:
	suggestion = decode_error(f'a: 0x{digits}').msg.removeprefix('Hexadecimal digits are uppercase: ')
	assert soml.loads(f'a: {suggestion}') == {'a': int(digits.replace('_', ''), 16)}


@pytest.mark.parametrize('digits', ['_a', 'a_', 'a__b', 'a_b_', '_'])
def test_a_hexadecimal_integer_with_a_misplaced_underscore_gets_no_uppercase_suggestion(digits: str) -> None:
	assert decode_error(f'a: 0x{digits}').msg == 'An underscore in a number must be between two digits'


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 0xffg', 'Invalid hexadecimal digit “g”'),
		('a: 0xFfz', 'Invalid hexadecimal digit “z”'),
		('a: 0xFG', 'Invalid hexadecimal digit “G”'),
		('a: 0o78', 'Invalid octal digit “8”'),
		('a: 0b102', 'Invalid binary digit “2”'),
	],
)
def test_an_invalid_digit_after_a_radix_prefix_is_the_one_named(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('text', 'exists'),
	[
		('2026-09-19', True),
		('2024-02-29', True),
		('0001-01-01', True),
		('9999-12-31', True),
		('2026-02-30', False),
		('2025-02-29', False),
		('1900-02-29', False),
		('2026-04-31', False),
		('2026-13-01', False),
		('2026-00-10', False),
		('2026-01-00', False),
		('0000-01-01', False),
		('0000-13-01', False),
		('2026-13-99', False),
		('1900-02-30', False),
	],
)
def test_date_validation_agrees_with_the_date_in_an_instant(text: str, exists: bool) -> None:  # noqa: FBT001 # A parameter.
	if exists:
		assert type(load_object(f'a: {text}')['a']) is date
		assert isinstance(load_object(f'a: {text}T00:00:00Z')['a'], datetime)
	else:
		date_reason = decode_error(f'a: {text}').msg.partition(': ')[2]
		instant_reason = decode_error(f'a: {text}T00:00:00Z').msg.partition(': ')[2]
		assert date_reason == instant_reason


LONE_SURROGATE = r'is not an escape, and a lone surrogate is not a Unicode scalar value. Write the character it is half of as one \u{…} escape'


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		# A JSON surrogate pair is one escape for the character it encodes.
		(r'a: "\uD83D\uDE00"', r'The four-digit \uD83D\uDE00 form is not an escape. Write \u{1f600}'),
		(r'a: "\ud83d\ude00"', r'The four-digit \ud83d\ude00 form is not an escape. Write \u{1f600}'),
		(r'a: "\uD800\uDC00"', r'The four-digit \uD800\uDC00 form is not an escape. Write \u{10000}'),
		(r'a: "\uDBFF\uDFFF"', r'The four-digit \uDBFF\uDFFF form is not an escape. Write \u{10ffff}'),
		# A lone surrogate has no escape to suggest, and neither has a pair in the wrong order or with a half that is missing.
		(r'a: "\uD83D"', rf'The four-digit \uD83D form {LONE_SURROGATE}'),
		(r'a: "\uDE00"', rf'The four-digit \uDE00 form {LONE_SURROGATE}'),
		(r'a: "\uDE00\uD83D"', rf'The four-digit \uDE00 form {LONE_SURROGATE}'),
		(r'a: "\uD83D\uD83D"', rf'The four-digit \uD83D form {LONE_SURROGATE}'),
		(r'a: "\uD83D\u0041"', rf'The four-digit \uD83D form {LONE_SURROGATE}'),
		(r'a: "\uD83D\u{de00}"', rf'The four-digit \uD83D form {LONE_SURROGATE}'),
		(r'a: "\uD83Dx"', rf'The four-digit \uD83D form {LONE_SURROGATE}'),
		# A carriage return has no escape either.
		(r'a: "\u000D"', r'The four-digit \u000D form is not an escape, and a carriage return (U+000D) cannot be represented'),
		(r'a: "\u000d"', r'The four-digit \u000d form is not an escape, and a carriage return (U+000D) cannot be represented'),
		(r'a: "\u0041"', r'The four-digit \u0041 form is not an escape. Write \u{41}'),
		# A braced escape with leading zeros is reported for its value first.
		(r'a: "\u{000d}"', r'A carriage return (U+000D) cannot be represented, so \u{000d} is not allowed'),
		(r'a: "\u{d}"', r'A carriage return (U+000D) cannot be represented, so \u{d} is not allowed'),
		(r'a: "\u{00d800}"', r'\u{00d800} is a surrogate, which is not a Unicode scalar value'),
		(r'a: "\u{0dfff}"', r'\u{0dfff} is a surrogate, which is not a Unicode scalar value'),
		(r'a: "\u{011000}"', r'A Unicode escape may not have leading zeros; write \u{11000}'),
		(r'a: "\u{00}"', r'A Unicode escape may not have leading zeros; write \u{0}'),
	],
)
def test_a_unicode_escape_error_does_not_suggest_an_escape_that_is_not_allowed(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


def test_every_four_digit_escape_suggests_an_escape_that_reads_as_the_character_json_reads() -> None:
	for code in range(0x10000):
		escape = rf'\u{code:04X}'
		message = decode_error(f'a: "{escape}"').msg
		suggestion = re.search(r'(?<=Write )\\u\{[0-9a-f]+\}', message)
		is_representable = code != 0x0D and not 0xD800 <= code <= 0xDFFF
		assert (suggestion is not None) == is_representable, escape

		if suggestion is not None:
			assert soml.loads(f'a: "{suggestion[0]}"') == {'a': json.loads(f'"{escape}"')}


def test_every_surrogate_pair_suggests_an_escape_that_reads_as_the_character_json_reads() -> None:
	for high in range(0xD800, 0xDC00, 0x31):
		for low in range(0xDC00, 0xE000, 0x29):
			escape = rf'\u{high:x}\u{low:x}'
			suggestion = re.search(r'(?<=Write )\\u\{[0-9a-f]+\}', decode_error(f'a: "{escape}"').msg)
			assert suggestion is not None, escape
			assert soml.loads(f'a: "{suggestion[0]}"') == {'a': json.loads(f'"{escape}"')}


def test_a_hexadecimal_integer_that_is_cut_for_the_error_gets_no_example_and_no_false_underscore_error() -> None:
	# The cut falls just after a correctly placed underscore, and the value is far outside the 64-bit range.
	assert decode_error('a: 0x' + 'a' * 997 + '_a').msg == 'Hexadecimal digits are uppercase'
	assert decode_error('a: 0x' + '0' * 990 + 'ff' + '0' * 20).msg == 'Hexadecimal digits are uppercase'


def test_a_bare_value_is_not_a_document_even_with_comments() -> None:
	assert decode_error('# note\n5 # five\n').msg.startswith('A bare value is not a document')
	assert decode_error("'''\nhello\n'''").msg.startswith('A bare value is not a document')


@pytest.mark.parametrize(
	('source', 'text'),
	[
		('name: John Smith', 'John Smith'),
		('name: John Smith # The owner', 'John Smith'),
		('name: John Smith \t\nisAdmin: true', 'John Smith'),
		('name: hello\u00a0 \t# The owner', 'hello\u00a0'),
		('name: hello\u2028 \t# The owner', 'hello\u2028'),
		('a: foo.bar', 'foo.bar'),
		('a: foo/bar', 'foo/bar'),
		('a: v1.2.3', 'v1.2.3'),
		('a: C# and F#', 'C# and F#'),
		('a: say "hi"', 'say "hi"'),
		('a: [foo bar, true]', 'foo bar'),
		('a: [foo bar]', 'foo bar'),
		('a: {b: foo bar}', 'foo bar'),
		('a: {b: foo bar, c: true}', 'foo bar'),
		('a: [\n\tfoo bar\n]', 'foo bar'),
	],
)
def test_a_suggested_quoted_string_holds_the_whole_unquoted_value(source: str, text: str) -> None:
	reason = decode_error(source).msg
	assert reason.endswith(f"A string value must be quoted, as in '{text}'"), reason
	# The suggestion is what was meant: with the text quoted, the document is valid and holds it.
	assert json.dumps(text) in json.dumps(soml.loads(source.replace(text, f"'{text}'", 1)))


def test_the_word_itself_is_still_what_is_unexpected() -> None:
	assert decode_error('name: John Smith').msg == "Unexpected “John”. A string value must be quoted, as in 'John Smith'"
	assert decode_error('a: hello').msg == "Unexpected “hello”. A string value must be quoted, as in 'hello'"


def test_a_value_that_holds_a_single_quote_has_no_literal_string_to_suggest() -> None:
	assert decode_error("a: it's here").msg == 'Unexpected “it”. A string value must be quoted'


def test_a_long_unquoted_value_is_cut_short_in_the_suggestion() -> None:
	words = ' '.join(['word'] * 1000)
	assert decode_error(f'a: {words}').msg == f"Unexpected “word”. A string value must be quoted, as in '{words[:40]}…'"


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('url: https://example.com', "Unexpected “https”. A string value must be quoted, as in 'https://example.com'"),
		('url: https://example.com/a?b=c # The API', "Unexpected “https”. A string value must be quoted, as in 'https://example.com/a?b=c'"),
		('host: localhost:8080', "Unexpected “localhost”. A string value must be quoted, as in 'localhost:8080'"),
		('a: {url: https://x, b: true}', "Unexpected “https”. A string value must be quoted, as in 'https://x'"),
		('a: x:y', "Unexpected “x”. A string value must be quoted, as in 'x:y'"),
		('msg: Error: file not found', "Unexpected “Error”. A string value must be quoted, as in 'Error: file not found'"),
		('a: [Note: x, true]', 'Expected a value, but found the key Note'),
		('a: {msg: Error: x}', "Unexpected “Error”. A string value must be quoted, as in 'Error: x'"),
		('a: b:', "Unexpected “b”. A string value must be quoted, as in 'b:'"),
		# Anywhere else, a key where a value should be is reported as a key, as when the value of an entry is left out, or in an array, even when it was meant as text.
		('a:\nb: 1', 'Expected a value, but found the key b'),
		('a:\nb:\t1', 'Expected a value, but found the key b'),
		('a:\nb:', 'Expected a value, but found the key b'),
		('a:\nb:1\nc: 2', 'Expected a value, but found the key b'),
		('{a:\nb:1}', 'Expected a value, but found the key b'),
		('a: [\n\t3\n\tc: 5\n]', 'Expected a value, but found the key c'),
		('[a:1]', 'Expected a value, but found the key a'),
		('x: [a:b, c]', 'Expected a value, but found the key a'),
		('a: [https://x, true]', 'Expected a value, but found the key https'),
	],
)
def test_a_word_after_the_colon_of_a_member_and_a_space_is_an_unquoted_string_not_a_key(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 1e999', '1e999 is too large to be a finite float. Use infinity if you mean it'),
		('a: 1.7976931348623159e308', '1.7976931348623159e308 is too large to be a finite float. Use infinity if you mean it'),
		('a: -1e999', '-1e999 is too large to be a finite float. Use -infinity if you mean it'),
		('a: -1.7976931348623159e308', '-1.7976931348623159e308 is too large to be a finite float. Use -infinity if you mean it'),
		('a: -1_000e1_000', '-1_000e1_000 is too large to be a finite float. Use -infinity if you mean it'),
	],
)
def test_a_float_that_is_too_large_suggests_the_infinity_of_its_sign(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason
	# The suggestion reads as the value that the literal rounds to.
	suggestion = re.search(r'(?<=Use )\S+', reason)
	assert suggestion is not None
	assert soml.loads(f'a: {suggestion[0]}') == {'a': float(source.removeprefix('a: ').replace('_', ''))}


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		# Quoting the words before the `:` would give a valid document with another meaning, or still an invalid one.
		('server localhost:8080', 'Expected “:” after the key, but found “l”'),
		('time 12:30', 'Expected “:” after the key, but found “1”'),
		('homepage https://example.com', 'Expected “:” after the key, but found “h”'),
		('the name: 1', "A bare key cannot contain spaces. Quote it, as in 'the name'"),
		('the name:\t1', "A bare key cannot contain spaces. Quote it, as in 'the name'"),
		('the name:\n\t1', "A bare key cannot contain spaces. Quote it, as in 'the name'"),
		('the name:', "A bare key cannot contain spaces. Quote it, as in 'the name'"),
		# Text directly after the `:` could be the value, as in `the name:1`, or part of it, as in `server localhost:8080`, so there is no hint.
		('the name:1', 'Expected “:” after the key, but found “n”'),
	],
)
def test_a_missing_colon_is_only_explained_as_a_key_with_spaces_when_the_colon_ends_the_key(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: .5', 'A number cannot begin with “.”; write a digit before it, as in 0.5'),
		('a: .5e3', 'A number cannot begin with “.”; write a digit before it, as in 0.5'),
		('a: .env', "Unexpected “.”. A string value must be quoted, as in '.env'"),
		('a: ./foo # The path', "Unexpected “.”. A string value must be quoted, as in './foo'"),
		('files: [.env, .git]', "Unexpected “.”. A string value must be quoted, as in '.env'"),
		('a: ...', "Unexpected “.”. A string value must be quoted, as in '...'"),
		('a: .', "Unexpected “.”. A string value must be quoted, as in '.'"),
	],
)
def test_a_value_that_starts_with_a_dot_is_only_a_number_when_a_digit_follows(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: Yes', '“Yes” is not a value. Booleans are written true and false, in lowercase'),
		('a: None # Unset', '“None” is not a value. Null is written null, in lowercase'),
		('a: [NaN, 1]', 'NaN is not representable. Use null for a missing value'),
		('a: Inf', '“Inf” is not a value. Infinity is written infinity, in lowercase'),
		# Following the hint would change the value or leave text behind, so the text is a string.
		('a: Yes please', "Unexpected “Yes”. A string value must be quoted, as in 'Yes please'"),
		('status: On hold', "Unexpected “On”. A string value must be quoted, as in 'On hold'"),
		('a: None of the above', "Unexpected “None”. A string value must be quoted, as in 'None of the above'"),
		('name: Nan Goldin', "Unexpected “Nan”. A string value must be quoted, as in 'Nan Goldin'"),
		('a: [Inf loop, 1]', "Unexpected “Inf”. A string value must be quoted, as in 'Inf loop'"),
	],
)
def test_a_keyword_hint_is_only_given_for_a_word_on_its_own(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('border: 1px solid black', "Invalid number “1px”. A string value must be quoted, as in '1px solid black'"),
		('font: 12pt Arial # Body', "Invalid number “12pt”. A string value must be quoted, as in '12pt Arial'"),
		('a: [1px solid, 2]', "Invalid number “1px”. A string value must be quoted, as in '1px solid'"),
		('a: 1px', "Invalid number “1px”. A string value must be quoted, as in '1px'"),
		("a: 3x it's", 'Invalid number “3x”. A string value must be quoted'),
		# The octal suggestion is only for the number on its own.
		('phone: 0412 345 678', "Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '0412 345 678'"),
		('date: 01/02/2026', "Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string: '01/02/2026'"),
		('mode: 0644', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o644, and an identifier, such as a ZIP code, as a string: '0644'"),
		# A literal string cannot hold a single quote, so then there is no example.
		("a: 01234 O'Brien", 'Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string'),
		("a: 0899 x'y", 'Leading zeros are not allowed in a decimal number. Write an identifier, such as a ZIP code, as a string'),
	],
)
def test_a_suggested_quoted_string_holds_the_whole_unquoted_value_that_starts_with_a_number(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		# A block comment after a space or a tab ends the value.
		('a: hello world /* note */', "Unexpected “hello”. A string value must be quoted, as in 'hello world'"),
		('a: [hello world\t/* note */]', "Unexpected “hello”. A string value must be quoted, as in 'hello world'"),
		('a: 1px solid /* note */', "Invalid number “1px”. A string value must be quoted, as in '1px solid'"),
		# A `/*` directly after the text is part of it.
		('a: src/*.js', "Unexpected “src”. A string value must be quoted, as in 'src/*.js'"),
	],
)
def test_a_block_comment_after_the_unquoted_value_is_not_part_of_the_suggestion(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('name=foo', 'Expected “:” after the key, but found “=”'),
		('port=8080', 'Expected “:” after the key, but found “=”'),
		('{a=1}', 'Expected “:” after the key, but found “=”'),
		('a = 1', 'Expected “:” after the key, but found “=”'),
		# Other characters directly after a key are still most likely part of it.
		('a$: 1', 'Expected “:” after the key, but found “$”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted'),
	],
)
def test_an_equals_sign_after_a_key_is_not_a_reason_to_quote_it(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		("a: #don't", f'Expected a value, but reached the end of the document. {COMMENT_HINT}'),
		("a: #'x'", f'Expected a value, but reached the end of the document. {COMMENT_HINT}'),
		("a: #it's\nb: 1", f'Expected a value, but found the key b. {COMMENT_HINT}'),
		("{a: #it's\n}", f'Expected a value, but found “}}”. {COMMENT_HINT}'),
		('a: #dont', f"Expected a value, but reached the end of the document. {COMMENT_HINT}, as in '#dont'"),
		('a: #say"hi"', f'Expected a value, but reached the end of the document. {COMMENT_HINT}, as in \'#say"hi"\''),
	],
)
def test_a_value_that_starts_with_a_hash_is_only_shown_quoted_when_a_literal_string_can_hold_it(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('value', ['#FFF', r'#a\b', '#say"hi"', '#1.5', '#é😀'])
def test_every_example_for_a_value_that_starts_with_a_hash_is_a_valid_string_that_holds_it(value: str) -> None:
	example = decode_error(f'a: {value}').msg.split('as in ', 1)[1]
	assert soml.loads(f'a: {example}') == {'a': value}


def test_a_huge_comment_where_a_value_was_meant_to_start_with_a_hash_is_cut_short() -> None:
	comment = f'#{"x" * 2_000_000}'
	assert re.search(r"“#” starts a comment, so a value that starts with “#” must be quoted, as in '#x+…'\Z", decode_error(f'a: {comment}').msg) is not None
	assert decode_error(f'{{a: {comment}\n}}').msg.endswith("…'")
	decode_error(f'a: {comment}\nb: 1')


def test_a_comment_where_a_value_was_meant_to_start_with_a_hash_is_read_up_to_the_limit_in_utf_16_code_units() -> None:
	# Each emoji is two UTF-16 code units, as JS counts, so the `'` after 500 of them is past the limit, and the example is given.
	assert decode_error(f"a: #{'😀' * 500}'x").msg.endswith("…'")
	assert decode_error(f"a: #{'😀' * 499}'x").msg.endswith(COMMENT_HINT)


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 0777777777777777777777', OCTAL_REASON.format(digits='777777777777777777777', text='0777777777777777777777')),
		('a: 0000777777777777777777777', OCTAL_REASON.format(digits='777777777777777777777', text='0000777777777777777777777')),
		# The largest octal int is 0o777777777777777777777, so one more digit is outside the 64-bit range.
		('a: 01000000000000000000000', IDENTIFIER_REASON.format(text='01000000000000000000000')),
		('a: 01777777777777777777777', IDENTIFIER_REASON.format(text='01777777777777777777777')),
		('a: 07777777777777777777777777', IDENTIFIER_REASON.format(text='07777777777777777777777777')),
	],
)
def test_a_suggested_octal_integer_is_in_range(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('digits', ['00', '07', '0644', '0755', '00007', '0777777777777777777777', '01000000000000000000000', '0' * 30])
def test_every_suggested_octal_integer_means_the_digits_in_octal(digits: str) -> None:
	suggestion = re.search(r'(?<=as )0o[0-9]+(?=,)', decode_error(f'a: {digits}').msg)

	if suggestion is None:
		assert int(digits, 8) > 9_223_372_036_854_775_807
	else:
		assert soml.loads(f'a: {suggestion[0]}') == {'a': int(digits, 8)}


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: [9,223,372,036,854,775,807]', f'{THOUSANDS_REASON.format(number="9,223,372,036,854,775,807")}. Write it as 9_223_372_036_854_775_807 or 9223372036854775807'),
		('a: [-9,223,372,036,854,775,808]', f'{THOUSANDS_REASON.format(number="-9,223,372,036,854,775,808")}. Write it as -9_223_372_036_854_775_808 or -9223372036854775808'),
		('a: [9,223,372,036,854,775,808]', THOUSANDS_REASON.format(number='9,223,372,036,854,775,808')),
		('a: [-9,223,372,036,854,775,809]', THOUSANDS_REASON.format(number='-9,223,372,036,854,775,809')),
		('a: [10,000,000,000,000,000,000]', THOUSANDS_REASON.format(number='10,000,000,000,000,000,000')),
	],
)
def test_a_number_with_thousands_separators_is_only_suggested_when_it_is_in_range(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('number', ['1,000', '-1,000', '999,000', '12,345,000', '9,223,372,036,854,775,807', '9,223,372,036,854,775,808', '-9,223,372,036,854,775,809'])
def test_every_number_suggested_for_thousands_separators_is_the_number_without_its_commas(number: str) -> None:
	value = int(number.replace(',', ''))
	suggestions = re.search(r'Write it as (\S+) or (\S+)\Z', decode_error(f'a: [{number}]').msg)

	if suggestions is None:
		assert not -9_223_372_036_854_775_808 <= value <= 9_223_372_036_854_775_807
		return

	for suggestion in suggestions.groups():
		assert soml.loads(f'a: [{suggestion}]') == {'a': [value]}


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 2024-02-29 14:00:00Z', f'{SEPARATOR}, as in 2024-02-29T14:00:00Z'),
		('a: 2026-01-01 14:00:00.123456789Z', f'{SEPARATOR}, as in 2026-01-01T14:00:00.123456789Z'),
		('a: 2026-02-30 14:00:00Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2025-02-29 14:00:00Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 24:00:00Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 14:60:00Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 14:00:60Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 14:00:00.1234567890Z', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 14:00:00-00:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 14:00:00+24:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		# In UTC, these fall outside the years 0001 to 9999.
		('a: 0001-01-01 00:00:00+01:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 9999-12-31 23:59:59-01:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		# A local time is only suggested in UTC when that is valid.
		(
			'a: 2024-02-29 14:00:00',
			f"{SEPARATOR}, and an instant needs the offset it was meant in, as in 2024-02-29T14:00:00Z for UTC. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2024-02-29 14:00:00'",
		),
		('a: 2026-02-30 14:00:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 2026-01-01 25:00:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
		('a: 0000-01-01 00:00:00', f'{SEPARATOR}. {INSTANT_FORMAT}'),
	],
)
def test_a_date_and_a_time_with_a_space_between_them_are_only_shown_as_an_instant_when_it_is_valid(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason
	instant = re.search(r'(?<=as in )\S+T\S+', reason)

	if instant is not None:
		assert is_valid(f'a: {instant[0]}')


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 1.5 us', UNIT_REASON.format(duration='1.5us', text='1.5 us')),
		('a: -5 m', UNIT_REASON.format(duration='-5m', text='-5 m')),
		('a: 1_000 ms', UNIT_REASON.format(duration='1_000ms', text='1_000 ms')),
		('a: 2562047 h', UNIT_REASON.format(duration='2562047h', text='2562047 h')),
		# Not a whole number of nanoseconds.
		('a: 1.5 ns', UNIT_REASON.format(duration='10s', text='1.5 ns')),
		('a: 0.0000000001 s', UNIT_REASON.format(duration='10s', text='0.0000000001 s')),
		# Zero has no negative spelling.
		('a: -0.0 s', UNIT_REASON.format(duration='10s', text='-0.0 s')),
		# Outside the 64-bit range of nanoseconds.
		('a: 2562048 h', UNIT_REASON.format(duration='10s', text='2562048 h')),
		('a: 3000000 h', UNIT_REASON.format(duration='10s', text='3000000 h')),
		# An exponent or a radix prefix cannot be part of a duration.
		('a: 1e3 ms', UNIT_REASON.format(duration='10s', text='1e3 ms')),
		('a: 0x10 s', UNIT_REASON.format(duration='10s', text='0x10 s')),
	],
)
def test_a_duration_without_the_space_is_only_suggested_when_it_is_valid(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('number', ['1', '-1', '0', '0.5', '1.5', '-1.5', '1_000', '1e3', '0x1F', '0o7', '0b1', '-0.0', '9223372036854775807', '0.000000001', '0.0000000001'])
@pytest.mark.parametrize('unit', ['h', 'm', 's', 'ms', 'us', 'ns'])
def test_every_duration_suggested_without_the_space_is_valid(number: str, unit: str) -> None:
	duration = re.search(r'(?<=as in )\S+(?=,)', decode_error(f'a: {number} {unit}').msg)
	assert duration is not None
	assert is_valid(f'a: {duration[0]}')


def test_a_huge_number_followed_by_a_space_and_a_unit() -> None:
	assert decode_error(f'a: 1.{"0" * 2_000_000} ms').msg.startswith('A unit cannot follow a number after a space')
	assert decode_error(f'a: 1.{"0_" * 1_000_000}5 ms').msg.startswith('A unit cannot follow a number after a space')
	assert decode_error(f'a: [1.{"0" * 2_000_000}5 MiB]').msg.startswith('A unit cannot follow a number after a space')


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 1µs', 'The unit for microseconds is written us, as in 1us'),
		('a: -1_000μs', 'The unit for microseconds is written us, as in -1_000us'),
		('a: 0.001µs', 'The unit for microseconds is written us, as in 0.001us'),
		# Not a whole number of nanoseconds.
		('a: 1.0005µs', 'The unit for microseconds is written us'),
		# Zero has no negative spelling.
		('a: -0.0µs', 'The unit for microseconds is written us'),
		# An exponent or a radix prefix cannot be part of a duration.
		('a: 1e3µs', 'The unit for microseconds is written us'),
		('a: 0x10µs', 'The unit for microseconds is written us'),
		# Outside the 64-bit range of nanoseconds.
		('a: 9223372036854775µs', 'The unit for microseconds is written us, as in 9223372036854775us'),
		('a: 9223372036854776µs', 'The unit for microseconds is written us'),
	],
)
def test_a_duration_in_microseconds_written_with_mu_is_only_suggested_with_us_when_it_is_valid(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason
	duration = re.search(r'(?<=as in )\S+\Z', reason)

	if duration is not None:
		assert is_valid(f'a: {duration[0]}')


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('[server]\nport: 1', TABLE_HEADER_REASON.format(name='server')),
		('# The server.\n[server]\nport: 1', TABLE_HEADER_REASON.format(name='server')),
		('\t[server]\nport: 1', TABLE_HEADER_REASON.format(name='server')),
		('[[servers]]\nport: 1', TABLE_HEADER_REASON.format(name='servers')),
		('a: 1\n[server]\nport: 1', TABLE_HEADER_REASON.format(name='server')),
		('a: {\n\t[server]\n}', TABLE_HEADER_REASON.format(name='server')),
		# An item of a nested array cannot be a member, so a word in brackets there is an unquoted string.
		('roles: [\n\t[admin]\n]', "Unexpected “admin”. A string value must be quoted, as in 'admin'"),
		('a: [\n\t1,\n\t[x]\n]', "Unexpected “x”. A string value must be quoted, as in 'x'"),
		('[\n\t1,\n\t[x]\n]', "Unexpected “x”. A string value must be quoted, as in 'x'"),
		('[\n\t[[x]]\n]', "Unexpected “x”. A string value must be quoted, as in 'x'"),
		# A value left out before a word in brackets on the next line, as YAML nests a flow sequence, is not a table either.
		('a:\n[server]\nport: 1', "Unexpected “server”. A string value must be quoted, as in 'server'"),
		('a:\n  [server]', "Unexpected “server”. A string value must be quoted, as in 'server'"),
		('{a:\n[server]\n}', "Unexpected “server”. A string value must be quoted, as in 'server'"),
		('[a-b_c]\nport: 1', TABLE_HEADER_REASON.format(name='a-b_c')),
		# The name is a valid key, and a bare key cannot contain a dot.
		('[a.b]\nport: 1', "Unexpected “a”. A string value must be quoted, as in 'a.b'"),
		('[[a.b]]\nport: 1', "Unexpected “a”. A string value must be quoted, as in 'a.b'"),
		('[a.]\nport: 1', "Unexpected “a”. A string value must be quoted, as in 'a.'"),
		('x: 1\n[a.b]\nport: 1', 'Expected a key, but found “[”'),
	],
)
def test_a_table_header_is_only_reported_where_a_table_could_be_written_instead(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize('name', ['a', 'a.b', 'a.b.c', 'a-b', '_a', 'a..b', 'a.', 'a.b.', 'a...b'])
def test_every_key_suggested_for_a_table_header_is_valid(name: str) -> None:
	suggestion = re.search(r'(?<=as in )\S+(?=: \{…\})', decode_error(f'[{name}]\nport: 1').msg)

	if suggestion is None:
		assert '.' in name
	else:
		assert suggestion[0] == name
		assert is_valid(f'{suggestion[0]}: {{port: 1}}')


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('server:\n  port: 80', 'Expected a value, but found the key port. Indentation does not nest objects, so write server: {port: …}'),
		("'a.b':\n  'c.d': 1", "Expected a value, but found the key \"c.d\". Indentation does not nest objects, so write 'a.b': {'c.d': …}"),
		(
			"'the server':\n  'the port': 80",
			"Expected a value, but found the key \"the port\". Indentation does not nest objects, so write 'the server': {'the port': …}",
		),
		('"it\'s":\n  b: 1', 'Expected a value, but found the key b. Indentation does not nest objects, so write "it\'s": {b: …}'),
		(r'"a\u{1}":' + '\n  b: 1', r'Expected a value, but found the key b. Indentation does not nest objects, so write "a\u{1}": {b: …}'),
		(
			'a:\n  ' + r'"\u{8}\u{c}\u{7f}": 1',
			r'Expected a value, but found the key "\b\f' + '\ufffd' + r'". Indentation does not nest objects, so write a: {"\u{8}\u{c}\u{7f}": …}',
		),
		('a:\n  "b\\\\c": 1', 'Expected a value, but found the key "b\\\\c". Indentation does not nest objects, so write a: {\'b\\c\': …}'),
	],
)
def test_the_keys_in_the_suggestion_for_an_indented_key_are_written_as_in_a_document(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(('parent', 'child'), [('a', 'b'), ("'a b'", r'"c\u{1}"'), (r'"\u{7f}"', r'"\t"'), ("'x.y'", "'é😀'")])
def test_the_suggestion_for_an_indented_key_nests_the_value_under_the_key(parent: str, child: str) -> None:
	match = re.search(r'so write (.+): \{(.+): …\}\Z', decode_error(f'{parent}:\n  {child}: 1').msg)
	assert match is not None
	braced, inner = match.groups()
	assert soml.loads(f'{braced}: {{{inner}: 1}}') == soml.loads(f'{parent}: {{{child}: 1}}')


def suggested_documents(reason: str) -> list[str]:
	"""The documents that each fix in a reason makes, where the fix is a value, a key, or an escape. A fix cut short with "…" is left out, because it is not the whole fix."""
	documents: list[str] = []

	def add(document: str) -> None:
		if '…' not in document:
			documents.append(document)

	for pattern in [
		r"(?<=as in )('[^\n']*')(?=\Z|[,.] | for )",
		# An offset, as in "written with a colon, as in +07:00", is part of an instant, not a value.
		r"(?<=as in )(?![+-][0-9]{2}:[0-9]{2}\Z)([^\s']+)(?=, and anything else|\Z)",
		r'(?<=uppercase: )(0x\S+)\Z',
		r'(?<=as )(0o\S+)(?=, and)',
		r'Write it as (\S+)(?= or \S+\Z)',
		r'Write it as \S+ or (\S+)\Z',
		r'(?<=Use )(-?infinity)(?= if you mean it)',
	]:
		for match in re.finditer(pattern, reason):
			add(f'a: {match[1]}')

	for match in re.finditer(r'(?<=as in )(\S+)(?=Z for UTC)', reason):
		add(f'a: {match[1]}Z')

	for match in re.finditer(r'(?<=[Ww]rite )(\\u\{[0-9a-f]+\})', reason):
		add(f'a: "{match[1]}"')

	indentation = re.search(r'so write (.+): \{(.+): …\}\Z', reason)

	if indentation is not None:
		add(f'{indentation[1]}: {{{indentation[2]}: 1}}')

	table = re.search(r'as in (.+): \{…\}\Z', reason)

	if table is not None:
		add(f'{table[1]}: {{}}')

	dot = re.search(r"Quote it, as in ('.+'), or use braces to nest, as in (.+): …(\}*)\Z", reason)

	if dot is not None:
		add(f'{dot[1]}: 1')
		add(f'{dot[2]}: 1{dot[3]}')

	return documents


def test_every_fix_that_an_error_suggests_is_valid_in_the_errors_for_mutations_of_every_valid_conformance_case() -> None:
	# Pieces that make the mistakes that the errors suggest fixes for.
	pieces = [
		' ',
		'\n',
		'\n  ',
		',',
		':',
		'.',
		'..',
		"'",
		'"',
		'#',
		'\\',
		'-',
		'_',
		'0',
		'00',
		'1',
		'9',
		'a',
		'e',
		'x',
		'T',
		' T',
		'Z',
		'z',
		's',
		'm',
		'h',
		' h',
		' ms',
		' s',
		' ns',
		'µs',
		'0x',
		'0o',
		'f',
		r'\u00',
		r'\u{0',
		r'\u{1}',
		'[',
		']',
		'[a]',
		'[a..b]',
		' 14:00:00',
		':00',
		'+02:00',
		'-00:00',
		',000',
		'0000',
	]
	generator = random.Random(1)
	checked = 0

	for path in sorted(CONFORMANCE.rglob('*.soml')):
		if path.name.endswith(('.canonical.soml', '.formatted.soml')):
			continue

		original = path.read_bytes().decode()

		# Each valid case with a piece inserted, or with a piece in place of one to three characters, at seeded random positions.
		for _ in range(25):
			position = generator.randint(0, len(original))
			piece = generator.choice(pieces)
			mutated = original[:position] + piece + original[position if generator.random() < 0.5 else position + generator.randint(1, 3) :]

			try:
				parse_document(mutated, truncates_nanoseconds=True)
			except soml.SOMLDecodeError as error:
				reason = error.msg
			else:
				continue

			for document in suggested_documents(reason):
				checked += 1
				assert is_valid(document), f'The error for {mutated!r}, {reason!r}, suggests {document!r}, which is invalid'

	# The mutations reach the suggestions, so the test checks something.
	assert checked > 1000, f'only {checked} suggestions were checked'


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		(r'a: "\u{D}"', r'A carriage return (U+000D) cannot be represented, so \u{D} is not allowed'),
		(r'a: "\u{00D}"', r'A carriage return (U+000D) cannot be represented, so \u{00D} is not allowed'),
		(r'a: "\u{D800}"', r'\u{D800} is a surrogate, which is not a Unicode scalar value'),
		(r'a: "\u{dFfF}"', r'\u{dFfF} is a surrogate, which is not a Unicode scalar value'),
		(r'a: "\u{FFFFFF}"', r'\u{FFFFFF} is above U+10FFFF, the largest Unicode scalar value'),
		(r'a: "\u{11000F}"', r'\u{11000F} is above U+10FFFF, the largest Unicode scalar value'),
		('a: """\n\t\\u{D}\n\t"""', r'A carriage return (U+000D) cannot be represented, so \u{D} is not allowed'),
		(r'"\u{D}": 1', r'A carriage return (U+000D) cannot be represented, so \u{D} is not allowed'),
		(r'a: "\u{E9}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{1F600}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{10FFFF}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{00E9}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{D7FF}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{E000}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{C}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{FFFFFFF}"', 'A Unicode escape uses lowercase hexadecimal digits'),
		(r'a: "\u{1234567}"', 'A Unicode escape has at most six hexadecimal digits'),
	],
)
def test_the_value_of_a_unicode_escape_with_uppercase_digits_is_checked_before_its_spelling(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 2026-09-19T14:00:00 Z', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: 2026-09-19T14:00:00 +02:00', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00+02:00"),
		('a: 2026-09-19T14:00:00.5 -07:30', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00.5-07:30"),
		('a: 2026-09-19T14:00:00\t\tZ # note', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: [2026-09-19T14:00:00 Z, 1]', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: {b: 2026-09-19T14:00:00 Z}', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: [\n\t2026-09-19T14:00:00 Z\n]', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: 2026-02-30T14:00:00 Z', "An instant's offset follows its time directly, without a space"),
		('a: 0001-01-01T00:00:00 +00:01', "An instant's offset follows its time directly, without a space"),
		('a: 2026-09-19T14:00:00 +24:00', "An instant's offset follows its time directly, without a space"),
		('a: 2026-09-19T14:00:00 +02:00 # é', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00+02:00"),
		(
			'a: 2026-09-19T14:00:00',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 # Z',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 Zulu',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 +02:00:00',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 +0200',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00\nZ: 1',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		('a: 2026-09-19T14:00 Z', 'Invalid instant “2026-09-19T14:00”. An instant is written as 2026-09-19T14:00:00Z, with an optional fraction of up to nine digits and an offset of Z or ±HH:MM'),
		('a: 2026-09-19T14:00:00Z Z', 'Expected a line break before the next entry, but found “Z”'),
		(
			'a: 2026-09-19T14:00:00 Montréal',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 é',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		('a: [2026-09-19 😀😀]', 'Expected “,”, a line break, or “]” after an array item, but found “😀”'),
		('a: 2026-09-19 Montréal', 'Expected a line break before the next entry, but found “M”'),
		('a: 2026-09-19T14:00 x日本', 'Invalid instant “2026-09-19T14:00”. An instant is written as 2026-09-19T14:00:00Z, with an optional fraction of up to nine digits and an offset of Z or ±HH:MM'),
		(
			'a: 2026-09-19T14:00:00 Zé',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 2026-09-19T14:00:00 +02:0é',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		(
			'a: 1979-05-27 07:32:00 Z',
			"The date and time separator in an instant is an uppercase “T”, not a space, and an instant needs the offset it was meant in, as in 1979-05-27T07:32:00Z for UTC. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '1979-05-27 07:32:00'",
		),
	],
)
def test_an_offset_after_a_space_is_the_offset_an_instant_was_meant_in(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('mac: 00:1A:2B:3C:4D:5E', "Invalid number “00:1A:2B:3C:4D:5E”. A string value must be quoted, as in '00:1A:2B:3C:4D:5E'"),
		('mac: 01:23:45:67:89:AB', "Invalid number “01:23:45:67:89:AB”. A string value must be quoted, as in '01:23:45:67:89:AB'"),
		('a: 0123:abcd::1', "Invalid number “0123:abcd::1”. A string value must be quoted, as in '0123:abcd::1'"),
		('a: [00:1A:2B, 1]', "Invalid number “00:1A:2B”. A string value must be quoted, as in '00:1A:2B'"),
		('a: -01:30', 'Invalid number “-01:30”'),
		('a: 01', "Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o1, and an identifier, such as a ZIP code, as a string: '01'"),
		('a: 01A', 'Leading zeros are not allowed in a decimal number'),
		('a: 00.5', 'Leading zeros are not allowed in a decimal number'),
		('a: 00:11:22:33:44:55', "Invalid number “00:11:22:33:44:55”. A value that contains “:” must be quoted, as in '00:11:22:33:44:55'"),
		('a: 08:30', 'A time of day is a string, so it must be quoted'),
		('a: 1:', 'Invalid number “1:”'),
		('a: 2001:db8::1', "Invalid number “2001:db8::1”. A string value must be quoted, as in '2001:db8::1'"),
	],
)
def test_a_value_with_a_colon_that_starts_with_a_zero_is_not_a_number_with_a_leading_zero(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 1y', 'Invalid duration 1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1y, 1]', 'Invalid duration 1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 2years', 'Invalid duration 2years: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [2years, 1]', 'Invalid duration 2years: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1year', 'Invalid duration 1year: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1year, 1]', 'Invalid duration 1year: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 3yr', 'Invalid duration 3yr: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [3yr, 1]', 'Invalid duration 3yr: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 3yrs', 'Invalid duration 3yrs: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [3yrs, 1]', 'Invalid duration 3yrs: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1Y', 'Invalid duration 1Y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1Y, 1]', 'Invalid duration 1Y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1YEAR', 'Invalid duration 1YEAR: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1YEAR, 1]', 'Invalid duration 1YEAR: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1.5y', 'Invalid duration 1.5y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1.5y, 1]', 'Invalid duration 1.5y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1_000y', 'Invalid duration 1_000y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1_000y, 1]', 'Invalid duration 1_000y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: -1y', 'Invalid duration -1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [-1y, 1]', 'Invalid duration -1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1y6m', 'Invalid duration 1y6m: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1y6m, 1]', 'Invalid duration 1y6m: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1h1y', 'Invalid duration 1h1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1h1y, 1]', 'Invalid duration 1h1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 1y1y', 'Invalid duration 1y1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: [1y1y, 1]', 'Invalid duration 1y1y: there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'),
		('a: 100yen', "Invalid number “100yen”. A string value must be quoted, as in '100yen'"),
		('a: 1yard', "Invalid number “1yard”. A string value must be quoted, as in '1yard'"),
		('a: 5yo', "Invalid number “5yo”. A string value must be quoted, as in '5yo'"),
		('a: 1yrsx', "Invalid number “1yrsx”. A string value must be quoted, as in '1yrsx'"),
		('a: 1yearsx', "Invalid number “1yearsx”. A string value must be quoted, as in '1yearsx'"),
		('a: 1y.5', "Invalid number “1y.5”. A string value must be quoted, as in '1y.5'"),
		('a: 1 y', "A unit cannot follow a number after a space. Write a duration without the space, as in 10s, and anything else, such as a size, as a string, as in '1 y'"),
	],
)
def test_a_duration_in_years_is_reported_as_one_and_a_word_that_only_starts_with_a_year_unit_is_a_string(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 7zip-bin-name', "Invalid number “7zip-bin-name”. A string value must be quoted, as in '7zip-bin-name'"),
		('a: 1byte', "Invalid number “1byte”. A string value must be quoted, as in '1byte'"),
		('a: 3-phase', "Invalid number “3-phase”. A string value must be quoted, as in '3-phase'"),
		('a: 1-time', "Invalid number “1-time”. A string value must be quoted, as in '1-time'"),
		('a: [100-pre, 1]', "Invalid number “100-pre”. A string value must be quoted, as in '100-pre'"),
		('a: 2e-e', "Invalid number “2e-e”. A string value must be quoted, as in '2e-e'"),
		('a: -verbose', 'Expected a digit or “infinity” after “-”'),
		('args: [-recursive]', 'Expected a digit or “infinity” after “-”'),
		('a: 1e', 'Expected digits after the exponent marker “e”'),
		('a: 1e-', 'Expected digits after the exponent marker “e”'),
		('a: -1e', 'Expected digits after the exponent marker “e”'),
		('a: 1.5e', 'Expected digits after the exponent marker “e”'),
		('a: 1.5e-', 'Expected digits after the exponent marker “e”'),
		('a: -1_0.5_0e', 'Expected digits after the exponent marker “e”'),
		('a: 0e', 'Expected digits after the exponent marker “e”'),
		('a: 0.0e-', 'Expected digits after the exponent marker “e”'),
	],
)
def test_only_a_number_ends_with_its_exponent_marker(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: -nano', 'Expected a digit or “infinity” after “-”'),
		('a: -nanny', 'Expected a digit or “infinity” after “-”'),
		('a: -info', 'Expected a digit or “infinity” after “-”'),
		('a: -INFO', 'Expected a digit or “infinity” after “-”'),
		('args: [-inform, PEM]', 'Expected a digit or “infinity” after “-”'),
		('a: -infinite', 'Expected a digit or “infinity” after “-”'),
		('a: -infinityx', 'Expected a digit or “infinity” after “-”'),
		('a: nano', "Unexpected “nano”. A string value must be quoted, as in 'nano'"),
		('a: info', "Unexpected “info”. A string value must be quoted, as in 'info'"),
		('a: -nan', 'NaN is not representable. Use null for a missing value'),
		('a: -NaN', 'NaN is not representable. Use null for a missing value'),
		('a: -NAN', 'NaN is not representable. Use null for a missing value'),
		('a: -inf', '“-inf” is not a value. Negative infinity is written -infinity'),
		('a: -Inf', '“-Inf” is not a value. Negative infinity is written -infinity'),
		('a: -INF', '“-INF” is not a value. Negative infinity is written -infinity'),
		('a: -Infinity', '“-Infinity” is not a value. Negative infinity is written -infinity'),
		('a: -INFINITY', '“-INFINITY” is not a value. Negative infinity is written -infinity'),
		('a: [-inf, 1]', '“-inf” is not a value. Negative infinity is written -infinity'),
		('a: [-Infinity, 1]', '“-Infinity” is not a value. Negative infinity is written -infinity'),
	],
)
def test_a_word_after_a_minus_is_only_nan_or_infinity_when_it_is_the_whole_word(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('speed: 100 km/h', 'Expected a line break before the next entry, but found “k”'),
		('a: 5 m/s', 'Expected a line break before the next entry, but found “m”'),
		('a: [5 m/s]', 'Expected “,”, a line break, or “]” after an array item, but found “m”'),
		('a: 2026-09-19T14:00:00/2026-09-20T15:00:00', 'An instant needs an offset: Z or ±HH:MM'),
		('a: [2026-09-19T14:00:00/P1D]', 'An instant needs an offset: Z or ±HH:MM'),
		(
			'a: 2026-09-19T14:00:00 Z/x',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		('a: 5 m /* c */', "A unit cannot follow a number after a space. Write a duration without the space, as in 5m, and anything else, such as a size, as a string, as in '5 m'"),
		('a: 5 m/* c */', "A unit cannot follow a number after a space. Write a duration without the space, as in 5m, and anything else, such as a size, as a string, as in '5 m'"),
		(
			'a: 2026-09-19T14:00:00/* c */',
			"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '2026-09-19T14:00:00', or add the offset it was meant in",
		),
		('a: 2026-09-19T14:00:00 Z/* c */', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('a: 2026-09-19T14:00:00 Z /* c */', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
		('timeout: 5 s // seconds', "A unit cannot follow a number after a space. Write a duration without the space, as in 5s, and anything else, such as a size, as a string, as in '5 s'"),
		('a: 5 m//c', "A unit cannot follow a number after a space. Write a duration without the space, as in 5m, and anything else, such as a size, as a string, as in '5 m'"),
		('a: 2026-09-19T14:00:00 Z//c', "An instant's offset follows its time directly, without a space, as in 2026-09-19T14:00:00Z"),
	],
)
def test_a_slash_only_ends_a_value_for_a_suggestion_when_it_starts_a_comment(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('a: 4k_video', "Invalid number “4k_video”. A string value must be quoted, as in '4k_video'"),
		('a: 2x_speed', "Invalid number “2x_speed”. A string value must be quoted, as in '2x_speed'"),
		('a: [5k_run, 2]', "Invalid number “5k_run”. A string value must be quoted, as in '5k_run'"),
		('a: 1_', 'An underscore in a number must be between two digits'),
		('a: 1__0', 'An underscore in a number must be between two digits'),
		('a: 1_.5', 'An underscore in a number must be between two digits'),
		('a: 1._5', 'An underscore in a number must be between two digits'),
		('a: 1_e5', 'An underscore in a number must be between two digits'),
		('a: 1e_5', 'An underscore in a number must be between two digits'),
		('a: 1e5_', 'An underscore in a number must be between two digits'),
		('a: 1.5_', 'An underscore in a number must be between two digits'),
		('a: -1_', 'An underscore in a number must be between two digits'),
		('a: 1_000_', 'An underscore in a number must be between two digits'),
		('a: 1e-_5', 'An underscore in a number must be between two digits'),
	],
)
def test_an_underscore_next_to_a_letter_is_part_of_a_word(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


@pytest.mark.parametrize(
	('source', 'reason'),
	[
		('price: 5EUR', "Invalid number “5EUR”. A string value must be quoted, as in '5EUR'"),
		('disk: 10EB', "Invalid number “10EB”. A string value must be quoted, as in '10EB'"),
		('a: 1.5Ex', "Invalid number “1.5Ex”. A string value must be quoted, as in '1.5Ex'"),
		('a: [3ED, 1]', "Invalid number “3ED”. A string value must be quoted, as in '3ED'"),
		('a: 1E5', 'An exponent marker is a lowercase “e”'),
		('a: 1E10', 'An exponent marker is a lowercase “e”'),
		('a: 1.5E-3', 'An exponent marker is a lowercase “e”'),
		('a: -2E7', 'An exponent marker is a lowercase “e”'),
		('a: 1_0E1_0', 'An exponent marker is a lowercase “e”'),
		('a: 1E', 'An exponent marker is a lowercase “e”'),
		('a: 2E-', 'An exponent marker is a lowercase “e”'),
		('a: 1.5E', 'An exponent marker is a lowercase “e”'),
	],
)
def test_an_uppercase_e_is_only_an_exponent_marker_where_one_goes(source: str, reason: str) -> None:
	assert decode_error(source).msg == reason


# Rejections, from the Swift port


@pytest.mark.parametrize(
	'source',
	[
		pytest.param('9223372036854775808', id='int64 max plus one'),
		pytest.param('-9223372036854775809', id='int64 min minus one'),
		pytest.param('99999999999999999999999999', id='a huge int'),
		pytest.param('1e999', id='a float too large'),
		pytest.param('-1e999', id='a negative float too large'),
		pytest.param('1.8e308', id='a float just past the maximum'),
		pytest.param('01', id='leading zeros'),
		pytest.param('00', id='two zeros'),
		pytest.param('-01', id='negative leading zeros'),
		pytest.param('0_1', id='a leading zero with an underscore'),
		pytest.param('00.5', id='a float with leading zeros'),
		pytest.param('01.5', id='a float with a leading zero'),
		pytest.param('01e3', id='an exponent float with a leading zero'),
		pytest.param('1e-05', id='a negative exponent with a leading zero'),
		pytest.param('1e00', id='an exponent of two zeros'),
		pytest.param('1e0_0', id='an exponent of zeros with an underscore'),
		pytest.param('1.5e07', id='a fraction with an exponent with a leading zero'),
		pytest.param('+0', id='a plus sign on zero'),
		pytest.param('+infinity', id='a plus sign on infinity'),
		pytest.param('1e', id='an exponent with no digits'),
		pytest.param('1e-', id='an exponent with only a sign'),
		pytest.param('-.5', id='a negative leading dot'),
		pytest.param('5.e3', id='a dot before an exponent'),
		pytest.param('10.23.1', id='a version'),
		pytest.param('-', id='a lone minus'),
		pytest.param('_1', id='a leading underscore'),
		pytest.param('1_', id='a trailing underscore'),
		pytest.param('1_.5', id='an underscore before the dot'),
		pytest.param('1._5', id='an underscore after the dot'),
		pytest.param('1_e5', id='an underscore before the exponent'),
		pytest.param('1e_5', id='an underscore after the exponent marker'),
		pytest.param('1e5_', id='an underscore at the end of an exponent'),
		pytest.param('0x_FF', id='an underscore after a prefix'),
		pytest.param('0xFF_', id='an underscore at the end of a hex int'),
		pytest.param('0xF__F', id='a doubled underscore in a hex int'),
		pytest.param('0o', id='an octal prefix with no digits'),
		pytest.param('0b', id='a binary prefix with no digits'),
		pytest.param('0xFf', id='mixed case hex digits'),
		pytest.param('0xFG', id='a non-hex letter'),
		pytest.param('0o8', id='an 8 in octal'),
		pytest.param('0b102', id='a 2 in binary'),
		pytest.param('0O7', id='an uppercase O prefix'),
		pytest.param('0B1', id='an uppercase B prefix'),
		pytest.param('-0x1', id='a negative hex int'),
		pytest.param('0x1.8p3', id='a hex float'),
		pytest.param('0b1.0', id='a float with a prefix'),
		pytest.param('50%', id='a percentage'),
		pytest.param('14:00', id='a time'),
		pytest.param('NaN', id='NaN'),
		pytest.param('-nan', id='negative nan'),
		pytest.param('Infinity', id='Infinity'),
		pytest.param('INFINITY', id='INFINITY'),
		pytest.param('-Infinity', id='negative Infinity'),
	],
)
def test_rejects_an_invalid_number(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'value: {source}')


@pytest.mark.parametrize(
	'source',
	[
		pytest.param('05m', id='a leading zero'),
		pytest.param('1.5h1.5m', id='two fractions'),
		pytest.param('1.5.5s', id='a version with a unit'),
		pytest.param('1.0000000001s', id='a fraction finer than a nanosecond'),
		pytest.param('1.0000005ms', id='a fraction of milliseconds finer than a nanosecond'),
		pytest.param(f'1.{"0" * 100}1s', id='a long fraction finer than a nanosecond'),
		pytest.param('.5s', id='a leading dot'),
		pytest.param('01.5s', id='a leading zero before a fraction'),
		pytest.param('1._5s', id='an underscore after the dot'),
		pytest.param('1_.5s', id='an underscore before the dot'),
		pytest.param('1.5e3s', id='an exponent in a fraction'),
		pytest.param('-0.0s', id='negative zero with a fraction'),
		pytest.param('2562047h47m16.854775808s', id='a fraction above the maximum'),
		pytest.param('-2562047h47m16.854775809s', id='a fraction below the minimum'),
		pytest.param('1w', id='a week'),
		pytest.param('1MS', id='an uppercase multi-letter unit'),
		pytest.param('1hm', id='a unit with no number'),
		pytest.param('1ms1s', id='milliseconds before seconds'),
		pytest.param('1m1m', id='a repeated unit'),
		pytest.param('+1h', id='a leading plus'),
		pytest.param('1__000ms', id='a doubled underscore'),
		pytest.param('1h_30m', id='an underscore before a later part'),
		pytest.param('-0h0m', id='negative zero in several parts'),
		pytest.param('9223372036854775808ns', id='above the maximum'),
		pytest.param('-9223372036854775809ns', id='below the minimum'),
		pytest.param('2562047h47m16s854ms775us808ns', id='parts that add up to above the maximum'),
		pytest.param('100000000000000000000000000000000000000000ns', id='a huge part'),
		pytest.param('1\u00b5s', id='the micro sign'),
		pytest.param('1\u03bcs', id='the Greek mu'),
		pytest.param('1 s', id='a space before the unit'),
		pytest.param('1h 30m', id='a space between parts'),
		pytest.param('0x10s', id='a radix prefix'),
		pytest.param('1.2.3-beta', id='a version with a suffix'),
		pytest.param('2.1.0b1', id='a version with a letter'),
		pytest.param('2562048h1h', id='a repeated unit after a part out of range'),
		pytest.param('30s-', id='a trailing minus'),
		pytest.param('1.5s5', id='a missing unit after a fraction'),
		pytest.param('0.5ns-', id='a trailing minus after a fraction finer than a nanosecond'),
		pytest.param('-9223372036854775809ns0.5ns', id='a repeated unit after a part out of range'),
		pytest.param('-0.1ns', id='negative zero with a fraction finer than a nanosecond'),
	],
)
def test_rejects_an_invalid_duration(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'value: {source}')


def test_a_bare_duration_is_not_a_document() -> None:
	assert decode_error('1h30m').msg.startswith('A bare value is not a document')


@pytest.mark.parametrize(
	'source',
	[
		pytest.param('2026-09-19 14:00:00Z', id='a space separator'),
		pytest.param('2026-09-19T14:00:00.5', id='no offset after a fraction'),
		pytest.param('26-09-19T14:00:00Z', id='a two digit year'),
		pytest.param('0000-01-01T00:00:00Z', id='year zero'),
		pytest.param('2026-00-01T00:00:00Z', id='month zero'),
		pytest.param('2026-13-01T00:00:00Z', id='month thirteen'),
		pytest.param('2026-01-00T00:00:00Z', id='day zero'),
		pytest.param('2026-01-32T00:00:00Z', id='January 32'),
		pytest.param('2026-04-31T00:00:00Z', id='April 31'),
		pytest.param('2026-06-31T00:00:00Z', id='June 31'),
		pytest.param('2026-09-31T00:00:00Z', id='September 31'),
		pytest.param('2026-11-31T00:00:00Z', id='November 31'),
		pytest.param('2024-02-30T00:00:00Z', id='February 30 in a leap year'),
		pytest.param('2026-01-01T00:60:00Z', id='minute 60'),
		pytest.param('2026-12-31T23:59:61Z', id='second 61'),
	],
)
def test_rejects_an_invalid_instant(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'value: {source}')


@pytest.mark.parametrize(
	'source',
	[
		pytest.param('a: 1\na: 1', id='the same key twice with the same value'),
		pytest.param('{a: 1, a: 2}', id='the same key inside braces'),
		pytest.param('\'a.b\': 1\n"a.b": 2', id='a quoted key with a dot twice'),
		pytest.param('a: {b: 1}\na: {c: 2}', id='an object twice'),
	],
)
def test_rejects_a_key_collision(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(source)


@pytest.mark.parametrize(
	'source',
	[
		pytest.param('a\t: 1', id='a tab before the colon'),
		pytest.param('{a : 1}', id='a space before the colon inside braces'),
		pytest.param('a# c\n: 1', id='a comment before the colon'),
		pytest.param('a/* c */: 1', id='a block comment before the colon'),
		pytest.param('a 1', id='a missing colon'),
		pytest.param('a', id='a missing colon at the end'),
		pytest.param('\u00e9: 1', id='a non-ASCII bare key'),
		pytest.param('$key: 1', id='a dollar sign'),
		pytest.param('@key: 1', id='an at sign'),
		pytest.param('a/b: 1', id='a key with a slash'),
		pytest.param('a+b: 1', id='a key with a plus'),
		pytest.param('a..b: 1', id='two dots'),
		pytest.param('a. b: 1', id='a space after a dot'),
		pytest.param('a .b: 1', id='a space before a dot'),
		pytest.param("'a\nb': 1", id='a key with a line break'),
		pytest.param(': 1', id='no key before the colon'),
		pytest.param('{}: 1', id='an object as a key'),
	],
)
def test_rejects_an_invalid_key(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(source)


@pytest.mark.parametrize(
	'source',
	[
		pytest.param(r'"\/"', id='slash'),
		pytest.param(r'"\b"', id='b'),
		pytest.param(r'"\f"', id='f'),
		pytest.param(r'"\v"', id='v'),
		pytest.param(r'"\0"', id='zero'),
		pytest.param(r'"\x41"', id='x41'),
		pytest.param(r'"\a"', id='a'),
		pytest.param(r'"\e"', id='e'),
		pytest.param(r'"\ "', id='a space'),
		pytest.param('"\\\u00e9"', id='a non-ASCII character'),
		pytest.param('"abc\\"', id='a backslash at the end'),
		pytest.param(r'"\U0001F600"', id='uppercase U'),
		pytest.param(r'"\u41"', id='u with no braces'),
		pytest.param(r'"\u{41"', id='u with an unclosed brace'),
		pytest.param(r'"\u{ 41}"', id='u with a space'),
		pytest.param(r'"\u{}"', id='u with no digits'),
		pytest.param(r'"\u{1F600}"', id='u with mixed case'),
		pytest.param(r'"\u{00}"', id='u with zeros only'),
		pytest.param(r'"\u{1000000}"', id='u with seven digits'),
		pytest.param(r'"\u{4g}"', id='u with a non-hex letter'),
		pytest.param(r'"\u{4_1}"', id='u with an underscore'),
		pytest.param(r'"\u{dbff}"', id='u dbff'),
		pytest.param(r'"\u{dc00}"', id='u dc00'),
		pytest.param(r'"\u{dfff}"', id='u dfff'),
		pytest.param(r'"\u{ffffff}"', id='u ffffff'),
	],
)
def test_rejects_an_invalid_escape(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'value: {source}')


@pytest.mark.parametrize(
	'source',
	[
		pytest.param("a: 'one\ntwo'", id='a literal string across lines'),
		pytest.param('a: "one\ntwo"', id='an escaped string across lines'),
		pytest.param('a: "escaped quote at the end\\"', id='an escaped quote at the end'),
		pytest.param("a: ''' \n\tone\n\t'''", id='trailing space on the opening line'),
		pytest.param("a: ''' # comment\n\tone\n\t'''", id='a comment on the opening line'),
		pytest.param("a: '''\n\tone\n", id='never closed'),
		pytest.param("a: '''\n\tone", id='never closed, at the end of the file'),
		pytest.param("a: '''", id='the opening delimiter at the end of the file'),
		pytest.param('a: """\n\tone\n', id='an escaped block never closed'),
		pytest.param("a: ''''\n\tone\n\t'''\n", id='a four-quote block closed by three'),
		pytest.param("a: '''\n    one\n\t'''", id='a line with spaces where the delimiter has a tab'),
		pytest.param("a: '''\n\t\tone\n\n\ttwo\n\t\t'''", id='a line with content indented less after a blank line'),
		pytest.param('a: """\n\t\\q\n\t"""', id='an invalid escape in an escaped block'),
		pytest.param('a: """\n\tone\\\n\ttwo\n\t"""', id='a backslash at the end of a line'),
		pytest.param("a: '''\n\tone\n\t'''x", id='text after the closing delimiter'),
		pytest.param("a: 'x'\u0301", id='a combining mark after the closing quote'),
	],
)
def test_rejects_an_invalid_string(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(source)


@pytest.mark.parametrize(
	'source',
	[
		pytest.param(' \n\t\n', id='only whitespace'),
		pytest.param('/* comment */', id='only a block comment'),
		pytest.param('1.5', id='a bare float'),
		pytest.param('true', id='a bare bool'),
		pytest.param('null', id='a bare null'),
		pytest.param('{a: 1}\n{b: 2}', id='two objects'),
		pytest.param('[1] [2]', id='two arrays'),
		pytest.param('{a: 1}\nb: 2', id='an object then an entry'),
		pytest.param('[1]\nb: 2', id='an array then an entry'),
		pytest.param('a: 1\n{b: 2}', id='an entry then an object'),
		pytest.param(']', id='a stray closing bracket'),
		pytest.param('a: 1 /* x */ b: 2', id='two entries separated by a comment'),
		pytest.param('a: 1 /*\n*/ b: 2', id='two entries separated by a multi-line block comment'),
		pytest.param('a: 1,\nb: 2', id='a comma between top-level entries'),
		pytest.param('a: 1,', id='a trailing comma at the top level'),
		pytest.param('{a: 1', id='an unclosed object'),
		pytest.param('a: {b: {c: 1}', id='an unclosed nested object'),
		pytest.param('[1}', id='a mismatched bracket'),
		pytest.param('{a: 1]', id='a mismatched brace'),
		pytest.param('[, 1]', id='a leading comma in an array'),
		pytest.param('{, a: 1}', id='a leading comma in an object'),
		pytest.param('[1,, 2]', id='a double comma in an array'),
		pytest.param('{a: 1,, b: 2}', id='a double comma in an object'),
		pytest.param('[,]', id='a lone comma in an array'),
		pytest.param('{a: }', id='a missing value inside braces'),
		pytest.param('[1 /*\n*/ 2]', id='a line break inside a block comment does not separate items'),
		pytest.param('{a: 1 /*\n*/ b: 2}', id='a line break inside a block comment does not separate members'),
		pytest.param('[1,\n,2]', id='a comma with no item before it across lines'),
		pytest.param('[[1] [2]]', id='a missing comma between arrays'),
		pytest.param("[1'a']", id='a string directly after a number'),
		pytest.param('a: hello world', id='an unquoted string with spaces'),
		pytest.param('a: nil', id='nil'),
		pytest.param('a: undefined', id='undefined'),
		pytest.param('a: ~', id='a tilde'),
		pytest.param('{"a" : 1}', id='a JSON-style object key'),
		pytest.param('- a', id='a YAML list item'),
		pytest.param("['table']\na: 1", id='an entry after a top-level array'),
		pytest.param('a: b: 1', id='a value that is a key-value pair'),
		pytest.param('/* /* */ */\na: 1', id='a nested block comment'),
		pytest.param('/* x *\na: 1', id='a block comment closed by half a terminator'),
		pytest.param('/* a /**/\na: 1', id='an opening right before the end'),
		pytest.param('/*/* */\na: 1', id='an opening at the start of the body'),
		pytest.param('a/* x */b: 1', id='a comment inside a key'),
		pytest.param('a: 1/* x */000', id='a comment inside a number'),
		pytest.param('// comment\na: 1', id='a C++ line comment'),
		pytest.param('; comment\na: 1', id='a semicolon comment'),
		pytest.param('/- a: 1\nb: 2', id='slash-dash before an entry'),
	],
)
def test_rejects_an_invalid_document(source: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(source)


def test_an_unterminated_block_comment_after_a_key_is_not_called_a_comment_before_the_colon() -> None:
	# The search for the end of the comment found nothing, and the check then looked at the start of the document.
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('a:{b /*')

	assert (error_info.value.msg, error_info.value.colno) == ('Expected “:” after the key, but found “/”', 6)


def test_an_unterminated_block_comment_after_a_key_ignores_a_colon_earlier_in_the_document() -> None:
	# The search for the end of the comment found nothing, and -1 + 2 pointed at the `:` at index 1.
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('x: 1\na /* c')

	assert (error_info.value.msg, error_info.value.lineno, error_info.value.colno) == ('Expected “:” after the key, but found “/”', 2, 3)


@pytest.mark.parametrize(
	('text', 'unit'),
	[
		('a: 1secondsabc', '“secondsabc”'),
		('a: 1secondsandmore', '“secondsand…”'),
	],
)
def test_an_unknown_duration_unit_is_shortened_to_ten_characters(text: str, unit: str) -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(text)

	assert error_info.value.msg == f'Invalid duration {text[3:]}: {unit} is not a unit. The units are h, m, s, ms, us, and ns, and a string must be quoted'


@pytest.mark.parametrize('number', ['0o_7', '0b_1', '0o7_', '0b1_', '0o1__2', '0b1__0', '1.0__1', '1e1__0'])
def test_an_underscore_must_be_between_two_digits_in_every_radix_and_part(number: str) -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(f'a: {number}')

	assert error_info.value.msg == 'An underscore in a number must be between two digits'


def test_a_fraction_that_underflows_is_an_error() -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('a: 0.1e-400')

	assert error_info.value.msg == '0.1e-400 is too small to be told apart from zero. Write 0.0 if you mean zero'


@pytest.mark.parametrize(
	('text', 'message'),
	[
		('é: 1', "Expected a key, but found “é”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in 'é'"),
		('$schema: 1', "Expected a key, but found “$”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '$schema'"),
		('a.$b: 1', DOT_IN_KEY_REASON),
		('}: 1', 'Expected a key, but found “}”'),
		('a/**/.b: 1', 'Expected “:” after the key, but found “/”'),
		('a$: 1', 'Expected “:” after the key, but found “$”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted'),
		('x: 1\ny/*', 'Expected “:” after the key, but found “/”'),
		('@a b: 1', 'Expected a key, but found “@”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted'),
		('a: 1\n}', 'Expected a key, but found “}”'),
		# A default-ignorable character renders as nothing, so it is named by code point and gets no hint, even when its category is a letter or a mark.
		('\u3164a: 1', 'Expected a key, but found U+3164'),
		('a\ufe0f: 1', 'Expected “:” after the key, but found U+FE0F'),
		('a: \U000e0100', 'Expected a value, but found U+E0100'),
		('\ue000: 1', 'Expected a key, but found U+E000'),
		('\U000f0000: 1', 'Expected a key, but found U+F0000'),
		('\u0378: 1', 'Expected a key, but found U+0378'),
		('\u0301: 1', "Expected a key, but found “\u0301”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '\u0301'"),
		('😀: 1', "Expected a key, but found “😀”. A key that contains characters other than letters, digits, “_”, and “-” must be quoted, as in '😀'"),
	],
)
def test_the_key_quoting_hint_is_given_only_for_characters_that_could_be_part_of_a_key(text: str, message: str) -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(text)

	assert error_info.value.msg == message


@pytest.mark.parametrize(
	('text', 'frame'),
	[
		pytest.param('a: 1\nb: 2\nc: 3\nd: nope', '  2 | b: 2\n  3 | c: 3\n> 4 | d: nope\n    |    ^', id='two lines of context'),
		pytest.param('nope', '> 1 | nope\n    |     ^', id='the first line'),
		pytest.param('a: 1\n\tb: x', '  1 | a: 1\n> 2 | \tb: x\n    | \t   ^', id='tabs are kept so the caret lines up'),
		pytest.param('\n' * 8 + 'a: 1\nb: x', '   8 |\n   9 | a: 1\n> 10 | b: x\n     |    ^', id='the gutter fits the line number'),
		pytest.param('a: "\\u{1b}[31m', '> 1 | a: "\\u{1b}[31m\n    |    ^', id='an escape written in the text is shown as written'),
	],
)
def test_code_frame(text: str, frame: str) -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(text)

	assert error_info.value.code_frame == frame


def test_code_frame_clips_a_long_line_around_the_error() -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('a: [' + ', '.join(['1'] * 200) + ', x]')

	line, caret = error_info.value.code_frame.split('\n')
	assert line.startswith('> 1 | …')
	assert line.endswith('x]')
	assert caret.index('^') == line.index('x]')


def test_the_clip_window_counts_code_points() -> None:
	# 64 code points, which is within the window, although it would be 124 UTF-16 code units, which is not.
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads("a: '" + '😀' * 60)

	assert '…' not in error_info.value.code_frame


def test_code_frame_replaces_characters_a_terminal_acts_on() -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('a: 1\n\u202eb: x')

	assert '\u202e' not in error_info.value.code_frame
	assert '\ufffd' in error_info.value.code_frame


@pytest.mark.parametrize('position', [0, 150, 299])
def test_clipped_code_frames_keep_the_caret_when_replacing_controls(position: int) -> None:
	line = '\u202e' * position + '!' + '\u202e' * (299 - position)
	error = soml.SOMLDecodeError('Invalid value', line, position)
	frame, caret = error.code_frame.split('\n')
	assert '\u202e' not in frame
	assert '\ufffd' in frame
	assert caret.index('^') == frame.index('!')
	assert len(frame) <= 108


def test_code_frame_clips_and_sanitizes_long_context_lines() -> None:
	line = "a: '" + '\u202e' * 1000 + "'"
	source = line + '\nb: nope'
	error = soml.SOMLDecodeError('Invalid value', source, len(line) + 4)
	assert error.code_frame == "  1 | a: '" + '\ufffd' * 96 + '…\n> 2 | b: nope\n    |    ^'


def test_code_frame_for_invalid_utf8_shows_the_text_before_the_error() -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(b'a: 1\nb: \xff')

	assert error_info.value.code_frame == '  1 | a: 1\n> 2 | b: \n    |    ^'


def test_code_frame_survives_pickling() -> None:
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads('a: nope')

	assert pickle.loads(pickle.dumps(error_info.value)).code_frame == error_info.value.code_frame  # noqa: S301 # The data was just made.


def test_code_frame_is_a_note() -> None:
	error = decode_error('a: 1\nb: nope\n')
	assert error.__notes__ == [error.code_frame]
	assert str(error) == "Unexpected “nope”. A string value must be quoted, as in 'nope' (at line 2, column 4)"


def test_code_frame_is_one_note_after_pickling_and_copying() -> None:
	error = decode_error('a: nope')
	restored = pickle.loads(pickle.dumps(error))  # noqa: S301 # The data was just made.

	for copied in (restored, copy.copy(error), copy.deepcopy(error)):
		assert copied.__notes__ == [error.code_frame]


def test_a_traceback_shows_the_code_frame() -> None:
	error = decode_error('a: 1\nb: nope\n')
	text = ''.join(traceback.format_exception(error))
	assert text.endswith(f'{error}\n{error.code_frame}\n')


@pytest.mark.parametrize('source', ['a: -infinity', 'a: -1e308', 'a: -1.7976931348623157e308'])
def test_a_negative_float_at_the_edge_of_the_range_is_accepted(source: str) -> None:
	assert load_object(source) == {'a': float(source.removeprefix('a: '))}


@pytest.mark.parametrize('source', ['a: -1e999', 'a: -1e1000', 'a: -1_0e999'])
def test_a_negative_float_that_is_too_large_suggests_the_negative_infinity(source: str) -> None:
	message = decode_error(source).msg
	assert message.endswith('Use -infinity if you mean it')
	assert load_object('a: -infinity') == {'a': -math.inf}


@pytest.mark.parametrize('source', ['a: 1e999', 'a: 1e1000'])
def test_a_positive_float_that_is_too_large_suggests_infinity(source: str) -> None:
	assert decode_error(source).msg.endswith('Use infinity if you mean it')
