"""Parser resource bounds and conversion boundaries."""

import tracemalloc
from datetime import timedelta

import pytest

import soml
from soml._parser import Parser, describe_bad_number


def test_huge_duration_integer_parts_are_rejected_without_copying_digits() -> None:
	text = '1_' * 100_000 + '1ns'
	parser = Parser('')
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='outside the 64-bit range'):
			parser.parse_duration(text, 0)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


def test_lowercase_hexadecimal_diagnostics_do_not_copy_the_whole_token() -> None:
	text = '0x' + 'a' * 200_000
	tracemalloc.start()
	try:
		reason = describe_bad_number(text, text)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert reason == 'Hexadecimal digits are uppercase'
	assert peak < 50_000


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('9_223_372_036_854_775_807', 9_223_372_036_854_775_807),
		('-9_223_372_036_854_775_808', -9_223_372_036_854_775_808),
		('0x7FFF_FFFF_FFFF_FFFF', 9_223_372_036_854_775_807),
		('0o777_777_777_777_777_777_777', 9_223_372_036_854_775_807),
		('0b' + '1_' * 62 + '1', 9_223_372_036_854_775_807),
		('1_000.2_5e-1', 100.025),
		('1_000_000_000us', timedelta(seconds=1000)),
		('0.0_0_1_0_0_0_0_0_0s', timedelta(milliseconds=1)),
	],
)
def test_builtin_numeric_conversions_preserve_grouped_number_values(text: str, expected: object) -> None:
	assert soml.loads(f'[{text}]') == [expected]


@pytest.mark.parametrize('text', ['0x8000_0000_0000_0000', '0o1_000_000_000_000_000_000_000', '0b1' + '0_' * 62 + '0'])
def test_each_radix_rejects_the_first_out_of_range_bit(text: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='outside the 64-bit range'):
		soml.loads(f'[{text}]')


@pytest.mark.parametrize('whitespace', [' \t' * 1000, '\n \t' * 1000, ' \t\n' * 1000])
def test_trivia_runs_preserve_separator_line_breaks(whitespace: str) -> None:
	source = f'[1{whitespace}2]'
	if '\n' in whitespace:
		assert soml.loads(source) == [1, 2]
	else:
		with pytest.raises(soml.SOMLDecodeError, match='Expected “,”'):
			soml.loads(source)


def test_block_comment_line_breaks_do_not_become_item_separators() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='inside a block comment'):
		soml.loads('[1 \t/*\n*/ \t2]')
	assert soml.loads('[1 \t/*\n*/ \t\n2]') == [1, 2]


@pytest.mark.parametrize('quote', ['"', '"""'])
def test_dense_escapes_keep_adjacent_text_and_final_escape(quote: str) -> None:
	text = r'\n\t\u{1f600}\\\"' * 1000
	source = f'[{quote}{text}{quote}]' if len(quote) == 1 else f'[{quote}\n\t{text}\n\t{quote}]'
	assert soml.loads(source) == ['\n\t😀\\"' * 1000]


def test_inline_block_delimiter_hints_do_not_copy_long_content_lines() -> None:
	source = 'a: """\n' + 'x' * 200_000 + '"""'
	parser = Parser(source)
	tracemalloc.start()
	try:
		reason = parser.describe_inline_closing_delimiter(7, '"', 3)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert 'at the end of line 2' in reason
	assert peak < 50_000


def test_space_separated_instant_hints_do_not_copy_long_time_tokens() -> None:
	source = 'a: 2026-01-01 12:34:' + '1' * 200_000
	parser = Parser(source)
	parser.index = 13
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='uppercase “T”'):
			parser.parse_instant('2026-01-01', 3)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


def test_spaced_unit_hints_do_not_copy_long_units() -> None:
	source = 'a: 1 ' + 'x' * 200_000
	parser = Parser(source)
	parser.index = 4
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='A unit cannot follow'):
			parser.diagnose_unit_after_space(3)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


@pytest.mark.parametrize('is_block', [False, True])
def test_adjacent_escapes_do_not_buffer_empty_text_chunks(*, is_block: bool) -> None:
	text = r'\n' * 100_000
	parser = Parser(text if is_block else f'"{text}"')
	tracemalloc.start()
	try:
		value = parser.unescape_line(text, 0) if is_block else parser.parse_escaped_string()
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert value == '\n' * 100_000
	assert peak < 1_200_000


def test_blank_block_lines_can_have_different_indentation() -> None:
	assert soml.loads("a: '''\n \n\t  x\n \t \n\t  y\n\t\n\t  '''") == {'a': 'x\n\ny'}


def test_leading_zero_diagnostics_do_not_copy_an_out_of_range_octal_candidate() -> None:
	text = '0' + '7' * 200_000
	tracemalloc.start()
	try:
		reason = describe_bad_number(text, text)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert reason.startswith('Leading zeros are not allowed')
	assert 'Write an octal number' not in reason
	assert peak < 50_000
