"""
Unit tests for `dumps()` and `dump()`, ported from soml-js's `test/stringify.ts` and from the Swift port's canonical form tests.

The JS tests that do not apply to Python are left out: `undefined`, symbols, getters, realms, `Date`, `Temporal` brand checks, and the `integers` option. Python raises `ValueError` where JS raises `TypeError` or `RangeError` for a value of the right type that cannot be written, and the nesting limit is 100.
"""

import array
import collections
import io
import math
import random
import struct
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import IntEnum, StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from helpers import EVERY_FEATURE_DOCUMENT, load_value, typed

import soml

MAX_DEPTH = 100


def nested_arrays(depth: int) -> list[Any]:
	value: list[Any] = []

	for _ in range(depth - 1):
		value = [value]

	return value


def nested_objects(depth: int) -> dict[str, Any]:
	value: dict[str, Any] = {}

	for _ in range(depth - 1):
		value = {'a': value}

	return value


# Layout


def test_a_top_level_object_has_no_braces_and_no_indentation() -> None:
	assert soml.dumps({'a': 1, 'b': 'x'}) == "a: 1\nb: 'x'\n"


def test_a_top_level_array() -> None:
	assert soml.dumps([1, 'x']) == "[\n\t1\n\t'x'\n]\n"


def test_an_empty_top_level_object_keeps_its_braces() -> None:
	assert soml.dumps({}) == '{}\n'


def test_an_empty_top_level_array() -> None:
	assert soml.dumps([]) == '[]\n'


def test_nesting_is_one_tab_per_level_with_one_item_on_each_line() -> None:
	assert soml.dumps({'a': {'b': [1, {'c': []}]}}) == 'a: {\n\tb: [\n\t\t1\n\t\t{\n\t\t\tc: []\n\t\t}\n\t]\n}\n'


def test_empty_nested_containers_are_written_on_one_line() -> None:
	assert soml.dumps({'a': {}, 'b': []}) == 'a: {}\nb: []\n'
	assert soml.dumps([[], {}]) == '[\n\t[]\n\t{}\n]\n'


@pytest.mark.parametrize('value', [{'a': 1}, [], {}, [[]], {'a': {'b': {}}}])
def test_the_output_ends_with_exactly_one_line_feed(value: Any) -> None:
	output = soml.dumps(value)
	assert output.endswith('\n')
	assert not output.endswith('\n\n')


def test_the_output_has_no_carriage_return_bom_double_space_or_trailing_space() -> None:
	text = soml.dumps(soml.loads(EVERY_FEATURE_DOCUMENT))
	assert '\r' not in text
	assert not text.startswith('\ufeff')
	assert '  ' not in text
	assert ' \n' not in text


def test_block_strings_are_never_written() -> None:
	assert soml.dumps(soml.loads("a: '''\n\tx\n\ty\n\t'''")) == 'a: "x\\ny"\n'


def test_a_braced_top_level_object_loses_its_braces() -> None:
	assert soml.dumps(soml.loads('{a: 1}')) == 'a: 1\n'


def test_comments_and_block_strings_are_gone() -> None:
	assert soml.dumps(soml.loads("# comment\na: {\n\tb: '''\n\t\tline one\n\t\tline two\n\t\t'''\n}")) == 'a: {\n\tb: "line one\\nline two"\n}\n'


def test_nesting_is_written_with_braces_and_tabs_without_commas() -> None:
	source = "server: {port: 8080, host: 'localhost'}\nlabels: ['b', 'a']\nempty-object: {}\nempty-array: []\nmatrix: [[1, 2], []]"
	expected = "server: {\n\tport: 8080\n\thost: 'localhost'\n}\nlabels: [\n\t'b'\n\t'a'\n]\nempty-object: {}\nempty-array: []\nmatrix: [\n\t[\n\t\t1\n\t\t2\n\t]\n\t[]\n]\n"
	assert soml.dumps(soml.loads(source)) == expected


def test_a_top_level_array_with_containers() -> None:
	assert soml.dumps(soml.loads('[1, {b: 2, a: 1}, []]')) == '[\n\t1\n\t{\n\t\tb: 2\n\t\ta: 1\n\t}\n\t[]\n]\n'


# Key order


def test_members_keep_their_order_by_default() -> None:
	assert soml.dumps({'name': 'api', 'description': 'x', 'version': 1}) == "name: 'api'\ndescription: 'x'\nversion: 1\n"


def test_nested_members_keep_their_order_by_default() -> None:
	assert soml.dumps({'z': {'b': 1, 'a': 2}, 'list': [{'y': 1, 'x': 2}]}) == 'z: {\n\tb: 1\n\ta: 2\n}\nlist: [\n\t{\n\t\ty: 1\n\t\tx: 2\n\t}\n]\n'


def test_the_default_is_a_fixed_point() -> None:
	text = soml.dumps(soml.loads(EVERY_FEATURE_DOCUMENT))
	assert soml.dumps(soml.loads(text)) == text


def test_the_default_differs_from_canonical_form_only_in_member_order() -> None:
	value = soml.loads(EVERY_FEATURE_DOCUMENT)
	text = soml.dumps(value)
	canonical = soml.dumps(value, canonical=True)
	assert text != canonical
	assert sorted(text.splitlines()) == sorted(canonical.splitlines())


def test_members_are_sorted_by_key() -> None:
	assert soml.dumps({'b': 1, 'a': 2, 'c': 3}, canonical=True) == 'a: 2\nb: 1\nc: 3\n'


def test_integer_like_keys_are_sorted_as_strings() -> None:
	assert soml.dumps({'b': 1, '10': 2, '9': 3, '1': 4}, canonical=True) == '1: 4\n10: 2\n9: 3\nb: 1\n'


def test_sorting_compares_code_points() -> None:
	# U+FF61 is one UTF-16 code unit, and U+1F600 is a surrogate pair starting with 0xD83D, so code unit order would put the emoji first.
	assert soml.dumps({'😀': 1, '\uff61': 2}, canonical=True) == "'\uff61': 2\n'😀': 1\n"
	assert soml.dumps({'\U00010000': 1, '\uffff': 2, '\ue000': 3, '\ud7ff': 4, 'z': 5}, canonical=True) == "z: 5\n'\ud7ff': 4\n'\ue000': 3\n'\uffff': 2\n'\U00010000': 1\n"


def test_two_astral_keys_are_compared_by_code_point() -> None:
	assert soml.dumps({'\U0001f601': 1, '\U0001f600': 2, '\U00020000': 3}, canonical=True) == "'\U0001f600': 2\n'\U0001f601': 1\n'\U00020000': 3\n"


def test_members_are_sorted_by_code_point_across_every_kind_of_key() -> None:
	value = {'z': 1, 'a': 2, 'B': 3, 'é': 4, '😀': 5, '\ufffd': 6, '1': 7, '-': 8, '': 9, 'a b': 10, 'a-b': 11, 'a_b': 12, 'aa': 13}
	assert list(soml.loads(soml.dumps(value, canonical=True))) == ['', '-', '1', 'B', 'a', 'a b', 'a-b', 'a_b', 'aa', 'z', 'é', '\ufffd', '😀']


def test_sorting_is_by_code_point_not_by_canonical_equivalence_or_locale() -> None:
	# "e" followed by U+0301 starts with "e", so it sorts before "f", and U+00E9 sorts after "f".
	assert soml.dumps({'e\u0301': 1, '\u00e9': 2, 'f': 3}, canonical=True) == "'e\u0301': 1\nf: 3\n'\u00e9': 2\n"


def test_a_key_that_is_a_prefix_of_another_comes_first() -> None:
	assert soml.dumps({'ab': 1, 'a': 2, 'a-b': 3}, canonical=True) == 'a: 2\na-b: 3\nab: 1\n'


def test_nested_objects_are_sorted_too() -> None:
	assert soml.dumps({'z': {'b': 1, 'a': 2}, 'list': [{'y': 1, 'x': 2}]}, canonical=True) == 'list: [\n\t{\n\t\tx: 2\n\t\ty: 1\n\t}\n]\nz: {\n\ta: 2\n\tb: 1\n}\n'


def test_arrays_keep_their_order() -> None:
	assert soml.dumps([3, 1, 2]) == '[\n\t3\n\t1\n\t2\n]\n'


@pytest.mark.parametrize('canonical', ['false', 1, None])
def test_a_canonical_option_that_is_not_a_bool_raises(canonical: Any) -> None:
	with pytest.raises(TypeError, match="The `canonical` option must be a bool, not '"):
		soml.dumps({'a': 1}, canonical=canonical)

	file = io.BytesIO()

	with pytest.raises(TypeError, match='canonical'):
		soml.dump({'a': 1}, file, canonical=canonical)

	assert file.getvalue() == b''


def test_insertion_order_does_not_change_canonical_form() -> None:
	assert soml.dumps({'a': 1, 'b': {'y': 1, 'x': 2}}, canonical=True) == soml.dumps({'b': {'x': 2, 'y': 1}, 'a': 1}, canonical=True)


# Keys


@pytest.mark.parametrize(
	('key', 'expected'),
	[
		('a', 'a'),
		('content-type', 'content-type'),
		('404', '404'),
		('_x', '_x'),
		('-', '-'),
		('true', 'true'),
		('null', 'null'),
		('infinity', 'infinity'),
		('1000', '1000'),
		('2024-01-01', '2024-01-01'),
		('', "''"),
		('a.b', "'a.b'"),
		('a b', "'a b'"),
		('é', "'é'"),
		('café', "'café'"),
		("it's", '"it\'s"'),
		('tab\there', '"tab\\there"'),
		('a\tb', '"a\\tb"'),
		('a\nb', '"a\\nb"'),
		('a:b', "'a:b'"),
		('#', "'#'"),
	],
)
def test_a_key_is_bare_when_it_can_be_and_quoted_otherwise(key: str, expected: str) -> None:
	text = soml.dumps({key: 1})
	assert text == f'{expected}: 1\n'
	assert soml.loads(text) == {key: 1}


def test_a_key_with_a_line_feed_is_written_as_an_escaped_string_when_nested() -> None:
	assert soml.dumps({'nested': {'\n': 1}}) == 'nested: {\n\t"\\n": 1\n}\n'


def test_a_key_with_a_lone_surrogate_raises() -> None:
	with pytest.raises(ValueError, match='key containing a lone surrogate'):
		soml.dumps({'\ud800': 1})


def test_a_key_with_a_carriage_return_raises() -> None:
	with pytest.raises(ValueError, match='key containing a carriage return'):
		soml.dumps({'a\rb': 1})


@pytest.mark.parametrize('key', [1, 1.5, None, True, ('a',), b'a'], ids=['int', 'float', 'None', 'bool', 'tuple', 'bytes'])
def test_a_key_that_is_not_a_str_raises_a_type_error(key: Any) -> None:
	with pytest.raises(TypeError, match=f'Object keys must be str, not {type(key).__name__!r}'):
		soml.dumps({key: 1})


def test_a_key_is_never_coerced_so_an_int_key_and_a_str_key_cannot_collide() -> None:
	value: Any = {'nested': {1: 'a'}}

	with pytest.raises(TypeError, match='must be str'):
		soml.dumps(value)


# Strings


def test_a_string_is_literal_by_default() -> None:
	assert soml.dumps({'a': 'x'}) == "a: 'x'\n"
	assert soml.dumps({'a': ''}) == "a: ''\n"


@pytest.mark.parametrize(
	('string', 'expected'),
	[
		('', "''"),
		('plain', "'plain'"),
		('C:\\Users', "'C:\\Users'"),
		('C:\\Users\\x', "'C:\\Users\\x'"),
		('say "hi"', '\'say "hi"\''),
		("it's", '"it\'s"'),
		('it\'s "quoted"', '"it\'s \\"quoted\\""'),
		('it\'s "x" \\', '"it\'s \\"x\\" \\\\"'),
		("it's C:\\dir", '"it\'s C:\\\\dir"'),
		('line\nbreak', '"line\\nbreak"'),
		('tab\there', '"tab\\there"'),
		('\x00', '"\\u{0}"'),
		('\x01', '"\\u{1}"'),
		('\x08', '"\\u{8}"'),
		('\x0b', '"\\u{b}"'),
		('\x0c', '"\\u{c}"'),
		('\x08\x0b\x0c', '"\\u{8}\\u{b}\\u{c}"'),
		('\x1b', '"\\u{1b}"'),
		('\x1f', '"\\u{1f}"'),
		('\x7f', '"\\u{7f}"'),
		('\x80', "'\x80'"),
		('\xa0', "'\xa0'"),
		('\u2028', "'\u2028'"),
		('\u2029', "'\u2029'"),
		('\ufeff', "'\ufeff'"),
		('\ufffe', "'\ufffe'"),
		('日本語', "'日本語'"),
		('😀', "'😀'"),
		('e\u0301', "'e\u0301'"),
		('# not a comment', "'# not a comment'"),
		("'", '"\'"'),
		('\\', "'\\'"),
		('"', "'\"'"),
		('\\\n', '"\\\\\\n"'),
	],
)
def test_a_string_uses_the_literal_form_unless_it_must_be_escaped(string: str, expected: str) -> None:
	text = soml.dumps({'a': string})
	assert text == f'a: {expected}\n'
	assert soml.loads(text) == {'a': string}


@pytest.mark.parametrize('code_point', [*range(0x0D), *range(0x0E, 0x20)])
def test_every_c0_control_character_is_escaped_in_the_shortest_lowercase_form(code_point: int) -> None:
	expected = {0x0A: '\\n', 0x09: '\\t'}.get(code_point, f'\\u{{{code_point:x}}}')
	assert soml.dumps({'a': chr(code_point)}) == f'a: "{expected}"\n'


def test_everything_else_is_written_literally() -> None:
	text = '\x80\x85\x9f\xa0\u2028\u2029\ufeff\ufffe\uffff\U0010ffffé😀日本'
	assert soml.dumps({'a': text}) == f"a: '{text}'\n"


@pytest.mark.parametrize('text', [chr(0xD800), chr(0xDFFF), f'a{chr(0xD83D)}', f'{chr(0xDE00)}b'])
def test_a_string_with_a_lone_surrogate_raises(text: str) -> None:
	with pytest.raises(ValueError, match='string containing a lone surrogate'):
		soml.dumps({'a': text})


def test_a_string_with_a_carriage_return_raises() -> None:
	with pytest.raises(ValueError, match='carriage return'):
		soml.dumps({'a': 'a\r\nb'})

	with pytest.raises(ValueError, match='carriage return'):
		soml.dumps(['\r'])


# Integers


def test_an_int_is_written_in_decimal() -> None:
	assert soml.dumps({'a': 0, 'b': -1, 'c': 255, 'd': 1_000_000}) == 'a: 0\nb: -1\nc: 255\nd: 1000000\n'


def test_the_int64_bounds() -> None:
	assert soml.dumps({'a': 2**63 - 1, 'b': -(2**63)}) == 'a: 9223372036854775807\nb: -9223372036854775808\n'


@pytest.mark.parametrize('value', [2**63, -(2**63) - 1, 10**100])
def test_an_int_outside_int64_raises(value: int) -> None:
	with pytest.raises(ValueError, match='outside the 64-bit range'):
		soml.dumps({'a': value})


def test_an_int_keeps_no_spelling_from_the_source() -> None:
	assert soml.dumps(soml.loads('a: 0xFF\nb: 0o644\nc: 0b1010\nd: 1_000')) == 'a: 255\nb: 420\nc: 10\nd: 1000\n'


def test_a_bool_and_none_are_written_as_keywords_not_as_ints() -> None:
	assert soml.dumps({'a': True, 'b': False, 'c': None, 'd': 1, 'e': 0}) == 'a: true\nb: false\nc: null\nd: 1\ne: 0\n'


# Floats


@pytest.mark.parametrize(
	('value', 'expected'),
	[
		(0.0, '0.0'),
		(-0.0, '0.0'),
		(1.0, '1.0'),
		(-1.0, '-1.0'),
		(0.5, '0.5'),
		(1.5, '1.5'),
		(30.0, '30.0'),
		(100.0, '100.0'),
		(0.1, '0.1'),
		(0.2, '0.2'),
		(0.1 + 0.2, '0.30000000000000004'),
		(1 / 3, '0.3333333333333333'),
		(2 / 3, '0.6666666666666666'),
		(123.456, '123.456'),
		(4.35, '4.35'),
		(1e16, '10000000000000000.0'),
		(1e20, '100000000000000000000.0'),
		(1e21, '1e21'),
		(1.5e21, '1.5e21'),
		(-1e21, '-1e21'),
		(1e22, '1e22'),
		(123_456_789_012_345_680_000.0, '123456789012345680000.0'),
		(1e-5, '0.00001'),
		(1e-6, '0.000001'),
		(1.5e-6, '0.0000015'),
		(0.000123, '0.000123'),
		(1e-7, '1e-7'),
		(1.5e-7, '1.5e-7'),
		(1e100, '1e100'),
		(1e-100, '1e-100'),
		(sys.float_info.max, '1.7976931348623157e308'),
		(-sys.float_info.max, '-1.7976931348623157e308'),
		(5e-324, '5e-324'),
		(sys.float_info.min, '2.2250738585072014e-308'),
		(sys.float_info.epsilon, '2.220446049250313e-16'),
		(9_007_199_254_740_991.0, '9007199254740991.0'),
		(float(2**53), '9007199254740992.0'),
		(math.pi, '3.141592653589793'),
		(math.inf, 'infinity'),
		(-math.inf, '-infinity'),
	],
)
def test_a_float_is_the_shortest_decimal_laid_out_like_ecmascript(value: float, expected: str) -> None:
	assert soml.dumps({'a': value}) == f'a: {expected}\n'
	assert typed(load_value(expected)) == typed(value)


def test_nan_raises() -> None:
	with pytest.raises(ValueError, match='NaN'):
		soml.dumps({'a': math.nan})

	with pytest.raises(ValueError, match='NaN'):
		soml.dumps([math.nan])

	with pytest.raises(ValueError, match='NaN'):
		soml.dumps([{'a': [math.nan]}])


@pytest.mark.parametrize('value', [0.0, 1.0, 1e15, 1e20, 1e21, 1e300, -5.0])
def test_a_float_never_reads_back_as_an_int(value: float) -> None:
	assert type(load_value(soml.dumps({'a': value}).removeprefix('a: ').removesuffix('\n'))) is float


def test_random_floats_read_back_exactly_and_are_never_longer_than_needed() -> None:
	generator = random.Random(3)  # A seeded generator, so a failure can be reproduced.

	for _ in range(20_000):
		# Any finite bit pattern, or a value of an everyday size.
		value = struct.unpack('<d', generator.randbytes(8))[0] if generator.getrandbits(1) else generator.uniform(-1e6, 1e6)

		if not math.isfinite(value):
			continue

		text = soml.dumps([value]).removeprefix('[\n\t').removesuffix('\n]\n')
		assert load_value(text) == value, text
		assert '.' in text or 'e' in text, text
		assert '+' not in text
		assert 'e0' not in text
		assert 'e-0' not in text
		assert '0e' not in text, text


def test_a_float_keeps_no_spelling_from_the_source() -> None:
	assert soml.dumps(soml.loads('a: 1_000.5\nb: 15e-1\nc: 0.50\nd: 1e5')) == 'a: 1000.5\nb: 1.5\nc: 0.5\nd: 100000.0\n'


# Instants


def test_an_aware_datetime_is_written_in_utc() -> None:
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, tzinfo=timezone(timedelta(hours=7)))}) == 'a: 2026-09-19T07:00:00Z\n'
	assert soml.dumps({'a': datetime(2026, 9, 19, 8, 30, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))}) == 'a: 2026-09-19T14:00:00Z\n'


def test_an_offset_that_is_not_a_whole_number_of_minutes_is_exact() -> None:
	# Python allows an offset with seconds and microseconds, as for local mean time, so the conversion to UTC cannot assume whole minutes.
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, tzinfo=timezone(timedelta(seconds=2589)))}) == 'a: 2026-09-19T13:16:51Z\n'
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, tzinfo=timezone(timedelta(seconds=-1, microseconds=1)))}) == 'a: 2026-09-19T14:00:00.999999Z\n'


def test_an_instant_from_the_source_is_written_in_utc_with_no_trailing_zeros() -> None:
	assert soml.dumps(soml.loads('a: 2026-09-19T16:00:00.500+02:00')) == 'a: 2026-09-19T14:00:00.5Z\n'
	assert soml.dumps(soml.loads('a: 2026-09-19T14:00:00.000Z')) == 'a: 2026-09-19T14:00:00Z\n'
	assert soml.dumps(soml.loads('a: 2026-09-19T14:00:00.000100Z')) == 'a: 2026-09-19T14:00:00.0001Z\n'


def test_the_fraction_drops_trailing_zeros_and_is_left_out_when_zero() -> None:
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, 0, 0, 100_000, tzinfo=UTC)}) == 'a: 2026-09-19T14:00:00.1Z\n'
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, 0, 0, 250_000, tzinfo=UTC)}) == 'a: 2026-09-19T14:00:00.25Z\n'
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, tzinfo=UTC)}) == 'a: 2026-09-19T14:00:00Z\n'
	assert soml.dumps({'a': datetime(2026, 9, 19, 14, 0, 0, 1, tzinfo=UTC)}) == 'a: 2026-09-19T14:00:00.000001Z\n'
	assert soml.dumps({'a': datetime(1970, 1, 1, tzinfo=UTC)}) == 'a: 1970-01-01T00:00:00Z\n'


def test_the_range_bounds() -> None:
	assert soml.dumps({'a': datetime.min.replace(tzinfo=UTC), 'b': datetime.max.replace(tzinfo=UTC)}) == 'a: 0001-01-01T00:00:00Z\nb: 9999-12-31T23:59:59.999999Z\n'


@pytest.mark.parametrize(
	'value',
	[
		datetime(1, 1, 1, tzinfo=timezone(timedelta(minutes=1))),
		datetime(1, 1, 1, 0, 59, 59, tzinfo=timezone(timedelta(hours=1))),
		datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-1))),
		datetime.max.replace(tzinfo=timezone(timedelta(hours=-23, minutes=-59))),
	],
)
def test_an_instant_outside_the_years_0001_to_9999_in_utc_raises_a_value_error_not_an_overflow_error(value: datetime) -> None:
	with pytest.raises(ValueError, match='outside the years 0001 to 9999'):
		soml.dumps({'a': value})


def test_a_naive_datetime_raises() -> None:
	with pytest.raises(ValueError, match='has no time zone'):
		soml.dumps({'a': datetime(2026, 9, 19, 14)})  # noqa: DTZ001 # A naive datetime, on purpose.


def test_a_date_is_written_without_a_time_or_timezone() -> None:
	assert soml.dumps({'a': date(2026, 9, 19)}) == 'a: 2026-09-19\n'


def test_two_spellings_of_one_instant_give_the_same_bytes() -> None:
	assert soml.dumps(soml.loads('a: 2026-09-19T16:00:00+02:00')) == soml.dumps(soml.loads('a: 2026-09-19T14:00:00.000Z'))


# Durations


@pytest.mark.parametrize(
	('value', 'expected'),
	[
		(timedelta(minutes=90), '1h30m'),
		(timedelta(milliseconds=1500), '1.5s'),
		(timedelta(hours=1, microseconds=1), '1h0.000001s'),
		(timedelta(microseconds=250), '0.00025s'),
		(timedelta(minutes=1, milliseconds=100), '1m0.1s'),
		(timedelta(minutes=-5), '-5m'),
		(timedelta(minutes=-90), '-1h30m'),
		(timedelta(milliseconds=-250), '-0.25s'),
		(timedelta(0), '0s'),
		(timedelta(days=1), '24h'),
		(timedelta(days=365), '8760h'),
		(timedelta(weeks=1), '168h'),
		(timedelta(seconds=30), '30s'),
		(timedelta(seconds=5400), '1h30m'),
		(timedelta(hours=1), '1h'),
		(timedelta(minutes=1, milliseconds=1), '1m0.001s'),
		(timedelta(hours=1, minutes=2, seconds=3, milliseconds=4, microseconds=5), '1h2m3.004005s'),
		(timedelta(hours=1, minutes=1, seconds=30), '1h1m30s'),
		(timedelta(minutes=6), '6m'),
		(timedelta(microseconds=2500), '0.0025s'),
		(timedelta(minutes=59, seconds=59, microseconds=999_999), '59m59.999999s'),
		(timedelta(microseconds=1), '0.000001s'),
		(timedelta(microseconds=-1), '-0.000001s'),
	],
)
def test_a_timedelta_is_written_in_canonical_form(value: timedelta, expected: str) -> None:
	assert soml.dumps({'a': value}) == f'a: {expected}\n'
	assert load_value(expected) == value


@pytest.mark.parametrize(
	('source', 'expected'),
	[
		('0ns', '0s'),
		('0h0m', '0s'),
		('0.0s', '0s'),
		('90m', '1h30m'),
		('5400s', '1h30m'),
		('1500ms', '1.5s'),
		('1_000ms', '1s'),
		('1h0m', '1h'),
		('1_000_000ns', '0.001s'),
		('1.50s', '1.5s'),
		('1.0s', '1s'),
		('1.500000000000000000000s', '1.5s'),
		('1.5h', '1h30m'),
		('0.1h', '6m'),
		('1h1.5m', '1h1m30s'),
		('-90m', '-1h30m'),
		('9223372036854775000ns', '2562047h47m16.854775s'),
		('-9223372036854775000ns', '-2562047h47m16.854775s'),
	],
)
def test_a_duration_keeps_no_spelling_from_the_source(source: str, expected: str) -> None:
	assert soml.dumps(soml.loads(f'a: {source}')) == f'a: {expected}\n'


def test_the_int64_bounds_of_a_duration() -> None:
	largest = timedelta(microseconds=(2**63 - 1) // 1000)
	smallest = timedelta(microseconds=-((2**63) // 1000))
	assert soml.dumps({'a': largest}) == 'a: 2562047h47m16.854775s\n'
	assert soml.dumps({'a': smallest}) == 'a: -2562047h47m16.854775s\n'

	with pytest.raises(ValueError, match='outside the 64-bit range'):
		soml.dumps({'a': largest + timedelta(microseconds=1)})

	with pytest.raises(ValueError, match='outside the 64-bit range'):
		soml.dumps({'a': smallest - timedelta(microseconds=1)})


@pytest.mark.parametrize('value', [timedelta(hours=2_562_048), timedelta.max, timedelta.min], ids=['2562048 hours', 'max', 'min'])
def test_a_timedelta_outside_int64_nanoseconds_raises(value: timedelta) -> None:
	with pytest.raises(ValueError, match='outside the 64-bit range of nanoseconds'):
		soml.dumps({'a': value})


def test_a_duration_key_is_never_a_duration() -> None:
	assert soml.dumps(soml.loads('1h30m: 1h30m')) == '1h30m: 1h30m\n'


# Other values


class Color(StrEnum):
	RED = 'red'

	def __str__(self) -> str:
		return 'not the value'


class Level(IntEnum):
	HIGH = 3

	def __str__(self) -> str:
		return 'not the value'


class Ratio(float):
	pass


class Name(str):
	__slots__ = ()


class Pair(Sequence[int]):
	def __getitem__(self, index: Any) -> Any:
		return [1, 2][index]

	def __len__(self) -> int:
		return 2


class Settings(Mapping[str, int]):
	def __getitem__(self, key: str) -> int:
		return {'b': 2, 'a': 1}[key]

	def __iter__(self) -> Iterator[str]:
		return iter(['b', 'a'])

	def __len__(self) -> int:
		return 2


def test_str_int_and_float_subclasses_are_written_as_their_value() -> None:
	assert soml.dumps({'color': Color.RED, 'level': Level.HIGH, 'ratio': Ratio(0.5), 'name': Name('x')}) == "color: 'red'\nlevel: 3\nratio: 0.5\nname: 'x'\n"


def test_a_str_subclass_key_is_written_as_its_value() -> None:
	assert soml.dumps({Color.RED: 1}) == 'red: 1\n'


def test_an_int_subclass_outside_int64_raises() -> None:
	class Big(int):
		pass

	with pytest.raises(ValueError, match='outside the 64-bit range'):
		soml.dumps({'a': Big(2**63)})


@pytest.mark.parametrize(
	('value', 'expected'),
	[
		pytest.param({'a': (1, 2)}, 'a: [\n\t1\n\t2\n]\n', id='tuple'),
		pytest.param({'a': collections.deque([1])}, 'a: [\n\t1\n]\n', id='deque'),
		pytest.param({'a': range(3)}, 'a: [\n\t0\n\t1\n\t2\n]\n', id='range'),
		pytest.param({'a': array.array('i', [1])}, 'a: [\n\t1\n]\n', id='array'),
		pytest.param({'a': Pair()}, 'a: [\n\t1\n\t2\n]\n', id='a custom Sequence'),
		pytest.param({'a': collections.OrderedDict(b=1, a=2)}, 'a: {\n\tb: 1\n\ta: 2\n}\n', id='OrderedDict'),
		pytest.param({'a': collections.defaultdict(int, {'x': 1})}, 'a: {\n\tx: 1\n}\n', id='defaultdict'),
		pytest.param({'a': collections.Counter({'x': 2})}, 'a: {\n\tx: 2\n}\n', id='Counter'),
		pytest.param({'a': types.MappingProxyType({'x': 1})}, 'a: {\n\tx: 1\n}\n', id='MappingProxyType'),
		# A key in several of the mappings is one member, with the value of the first mapping that has it.
		pytest.param({'a': collections.ChainMap({'x': 1}, {'y': 2, 'x': 3})}, 'a: {\n\ty: 2\n\tx: 1\n}\n', id='ChainMap'),
		pytest.param({'a': Settings()}, 'a: {\n\tb: 2\n\ta: 1\n}\n', id='a custom Mapping'),
		pytest.param((1, 'x'), "[\n\t1\n\t'x'\n]\n", id='a top-level tuple'),
		pytest.param(types.MappingProxyType({'a': 1}), 'a: 1\n', id='a top-level MappingProxyType'),
		pytest.param(Settings(), 'b: 2\na: 1\n', id='a top-level custom Mapping'),
	],
)
def test_any_mapping_and_any_sequence_but_text_and_bytes_are_accepted(value: Any, expected: str) -> None:
	assert soml.dumps(value) == expected


@pytest.mark.parametrize(
	('value', 'type_name'),
	[
		pytest.param(b'x', 'bytes', id='bytes'),
		pytest.param(bytearray(b'x'), 'bytearray', id='bytearray'),
		pytest.param(memoryview(b'x'), 'memoryview', id='memoryview'),
		# A sequence of one-character `UserString`s, each of which is one too, so it would never end as an array.
		pytest.param(collections.UserString('x'), 'UserString', id='UserString'),
		pytest.param(collections.UserString(''), 'UserString', id='empty UserString'),
		pytest.param(set(), 'set', id='set'),
		pytest.param(frozenset(), 'frozenset', id='frozenset'),
		pytest.param(object(), 'object', id='object'),
		pytest.param(1j, 'complex', id='complex'),
		pytest.param(Decimal('1.5'), 'Decimal', id='Decimal'),
		pytest.param(Fraction(1, 2), 'Fraction', id='Fraction'),
		pytest.param(time(1), 'time', id='time'),
		pytest.param(Path('a'), type(Path('a')).__name__, id='Path'),
		pytest.param(len, 'builtin_function_or_method', id='function'),
		pytest.param(iter([1]), 'list_iterator', id='iterator'),
		pytest.param((item for item in [1]), 'generator', id='generator'),
	],
)
def test_an_unsupported_value_raises_a_type_error_that_names_its_type(value: Any, type_name: str) -> None:
	with pytest.raises(TypeError, match=f"^Object of type '{type_name}' is not SOML serializable$"):
		soml.dumps({'a': value})


@pytest.mark.parametrize(
	'value',
	['x', 1, 1.5, True, None, date(2026, 9, 19), datetime.now(UTC), timedelta(1), b'x', set(), 'xy', collections.UserString('xy')],
	ids=['str', 'int', 'float', 'bool', 'None', 'date', 'datetime', 'timedelta', 'bytes', 'set', 'two-character str', 'UserString'],
)
def test_the_top_level_value_must_be_a_dict_or_a_list(value: Any) -> None:
	with pytest.raises(TypeError, match=f'^The top-level value must be a dict or a list, because a document is a collection, not {type(value).__name__!r}$'):
		soml.dumps(value)


# Structure


def test_a_circular_reference_raises() -> None:
	value: dict[str, Any] = {'a': 1}
	value['self'] = value

	with pytest.raises(ValueError, match=r'^Cannot serialize a circular structure$'):
		soml.dumps(value)

	items: list[Any] = []
	items.append(items)

	with pytest.raises(ValueError, match=r'^Cannot serialize a circular structure$'):
		soml.dumps(items)

	deep: dict[str, Any] = {'a': {'b': {}}}
	deep['a']['b']['c'] = deep['a']

	with pytest.raises(ValueError, match=r'^Cannot serialize a circular structure$'):
		soml.dumps(deep)


def test_a_shared_reference_that_is_not_circular_is_written_twice() -> None:
	shared = {'x': 1}
	assert soml.dumps({'a': shared, 'b': shared, 'c': [shared, shared]}) == 'a: {\n\tx: 1\n}\nb: {\n\tx: 1\n}\nc: [\n\t{\n\t\tx: 1\n\t}\n\t{\n\t\tx: 1\n\t}\n]\n'


def test_a_failed_serialization_does_not_poison_the_next_one() -> None:
	value: dict[str, Any] = {'a': {}}
	value['a']['b'] = value

	with pytest.raises(ValueError, match='circular structure'):
		soml.dumps(value)

	fine: dict[str, Any] = {'a': {}}
	assert soml.dumps({'x': fine, 'y': fine}) == 'x: {\n\ta: {}\n}\ny: {\n\ta: {}\n}\n'


def test_nesting_up_to_the_limit_is_written() -> None:
	text = soml.dumps(nested_arrays(MAX_DEPTH))
	assert text.startswith('[\n')
	assert soml.dumps(soml.loads(text)) == text


def test_nesting_past_the_limit_raises() -> None:
	with pytest.raises(ValueError, match=f'nested more than {MAX_DEPTH} levels'):
		soml.dumps(nested_arrays(MAX_DEPTH + 1))


def test_the_top_level_object_counts_as_a_level() -> None:
	value = nested_objects(MAX_DEPTH)
	assert soml.dumps(value).startswith('a: {\n')

	with pytest.raises(ValueError, match=f'nested more than {MAX_DEPTH} levels'):
		soml.dumps({'a': value})


def test_a_scalar_at_the_deepest_level_is_written() -> None:
	value: Any = 1

	for _ in range(MAX_DEPTH):
		value = [value]

	assert typed(soml.loads(soml.dumps(value))) == typed(value)


def test_the_nesting_limit_matches_the_parser() -> None:
	for depth in [MAX_DEPTH, MAX_DEPTH + 1]:
		text = '[' * depth + ']' * depth

		try:
			soml.loads(text)
		except soml.SOMLDecodeError:
			is_readable = False
		else:
			is_readable = True

		try:
			soml.dumps(nested_arrays(depth))
		except ValueError:
			is_writable = False
		else:
			is_writable = True

		assert is_readable == is_writable == (depth <= MAX_DEPTH)


def test_a_very_deep_value_raises_rather_than_hitting_the_recursion_limit() -> None:
	with pytest.raises(ValueError, match='nested more than'):
		soml.dumps(nested_arrays(100_000))


# Canonical form


def test_two_spellings_of_one_value_give_the_same_bytes() -> None:
	first = "# A comment.\nserver: {port: 0x1F90, host: \"localhost\"}\nat: 2026-09-19T16:00:00+02:00\ntags: ['a', 'b',]"
	second = '{tags: ["a", "\\u{62}"], at: 2026-09-19T14:00:00.000Z, server: {host: \'localhost\', port: 8080}}'
	assert soml.dumps(soml.loads(first), canonical=True) == soml.dumps(soml.loads(second), canonical=True)


def test_canonical_form_is_a_fixed_point() -> None:
	text = soml.dumps(soml.loads(EVERY_FEATURE_DOCUMENT), canonical=True)
	assert soml.dumps(soml.loads(text), canonical=True) == text


def test_a_flat_object_is_sorted_by_key() -> None:
	assert soml.dumps(soml.loads("b: 2\na: 1\nc: 'x'"), canonical=True) == "a: 1\nb: 2\nc: 'x'\n"


def test_a_value_reads_back_with_its_types() -> None:
	value: dict[str, soml.Serializable] = {
		'string': 'x',
		'int': 1,
		'float': 1.0,
		'bool': True,
		'null': None,
		'instant': datetime(2026, 9, 19, 14, 0, 0, 123_456, tzinfo=UTC),
		'duration': timedelta(minutes=90, microseconds=1),
		'array': [1, 1.0, [], {}],
		'object': {'nested': {'deep': [False]}},
	}
	assert typed(soml.loads(soml.dumps(value))) == typed(value)


# dump()


def test_dump_writes_utf8_bytes_to_a_binary_file() -> None:
	file = io.BytesIO()
	soml.dump({'name': 'é😀', 'port': 8080}, file)
	assert file.getvalue() == "name: 'é😀'\nport: 8080\n".encode()


def test_dump_keeps_the_member_order_or_writes_canonical_form() -> None:
	file = io.BytesIO()
	soml.dump({'b': 1, 'a': 2}, file)
	assert file.getvalue() == b'b: 1\na: 2\n'

	file = io.BytesIO()
	soml.dump({'b': 1, 'a': 2}, file, canonical=True)
	assert file.getvalue() == b'a: 2\nb: 1\n'


def test_dump_writes_a_file_that_load_reads_back(tmp_path: Path) -> None:
	path = tmp_path / 'config.soml'
	value: dict[str, soml.Serializable] = {'name': 'é', 'launch': date(2026, 11, 1), 'at': datetime(2026, 9, 19, 14, tzinfo=UTC), 'timeout': timedelta(seconds=30)}

	with path.open('wb') as file:
		soml.dump(value, file)

	with path.open('rb') as file:
		assert typed(soml.load(file)) == typed(value)


def test_dump_writes_nothing_when_it_raises() -> None:
	file = io.BytesIO()

	with pytest.raises(ValueError, match='NaN'):
		soml.dump({'a': 1, 'b': math.nan}, file)

	assert file.getvalue() == b''


def test_dump_to_a_text_file_says_to_open_it_in_binary_mode(tmp_path: Path) -> None:
	path = tmp_path / 'config.soml'

	with path.open('w') as file, pytest.raises(TypeError, match=r"open\('config.soml', 'wb'\)"):
		soml.dump({'a': 1}, file)  # type: ignore[arg-type]

	assert path.read_bytes() == b''

	with pytest.raises(TypeError, match='binary mode'):
		soml.dump({'a': 1}, io.StringIO())  # type: ignore[arg-type]


def test_an_int_subclass_is_written_as_its_value_even_when_it_overrides_int() -> None:
	class Overriding(int):
		def __int__(self) -> int:
			return 5

		def __index__(self) -> int:
			return 5

	assert soml.dumps({'a': Overriding(3)}) == 'a: 3\n'


def test_a_float_subclass_is_written_as_its_value_even_when_it_overrides_float() -> None:
	class Overriding(float):
		def __float__(self) -> float:
			return 9.0

	assert soml.dumps({'a': Overriding(1.5)}) == 'a: 1.5\n'


def test_two_keys_that_are_the_same_string_are_refused() -> None:
	class AlwaysDifferent(str):
		__slots__ = ()

		def __eq__(self, other: object) -> bool:
			return False

		def __hash__(self) -> int:
			return 1

	with pytest.raises(ValueError, match="Cannot serialize two keys that are the same string: 'a'"):
		soml.dumps({AlwaysDifferent('a'): 1, 'a': 2})


def test_datetime_and_timedelta_subclasses_are_written_as_their_value_even_when_they_override_operators() -> None:
	class OddDuration(timedelta):
		def __floordiv__(self, other: object) -> int:  # type: ignore[override]
			return 0

	class OddInstant(datetime):
		def __sub__(self, other: object) -> timedelta:  # type: ignore[override]
			return timedelta(0)

	assert soml.dumps({'a': OddDuration(seconds=5), 'b': OddInstant(2026, 1, 1, tzinfo=UTC)}) == 'a: 5s\nb: 2026-01-01T00:00:00Z\n'
