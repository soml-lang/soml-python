"""Focused conversion and resource-bound regressions for the second parser pass."""

import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest

import soml
from soml._parser import Parser, without_comments


@pytest.mark.parametrize('suffix', ['s', 'ns'])
def test_huge_zero_duration_fractions_do_not_copy_the_digit_run(suffix: str) -> None:
	text = '0.' + '0_' * 100_000 + f'0{suffix}'
	parser = Parser('')
	tracemalloc.start()
	try:
		assert parser.parse_duration(text, 0).total_seconds() == 0
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


def test_huge_duration_units_do_not_copy_the_unit() -> None:
	text = '1' + 'h' * 200_000
	parser = Parser('')
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='is not a unit'):
			parser.parse_duration(text, 0)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('0001-01-01T23:59:00+23:59', datetime(1, 1, 1, tzinfo=UTC)),
		('9999-12-31T00:00:59.999999000-23:59', datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
		('1970-01-01T00:00:00.000001000Z', datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)),
		('1969-12-31T23:59:59.999999000Z', datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
	],
)
def test_instant_conversion_preserves_extreme_offsets_and_epoch_sides(text: str, expected: datetime) -> None:
	assert soml.loads(f'[{text}]') == [expected]


@pytest.mark.parametrize('text', ['2026-01-01T00:00:00+00:60', '2026-01-01T00:00:00-00:00', '2026-01-01T00:00:00.000000001Z'])
def test_python_isoformat_extensions_do_not_expand_the_soml_grammar(text: str) -> None:
	with pytest.raises(soml.SOMLDecodeError):
		soml.loads(f'[{text}]')


def test_unknown_word_diagnostics_do_not_copy_the_whole_word() -> None:
	parser = Parser('[' + 'x' * 200_000 + ']')
	parser.index = 1
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='string value must be quoted'):
			parser.fail_unexpected_value()
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


def test_microsecond_unit_diagnostics_do_not_copy_a_long_radix_literal() -> None:
	parser = Parser('0x' + '0' * 200_000 + '1µs')
	parser.index = parser.length - 2
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='unit for microseconds is written us'):
			parser.fail_after_value(0, is_number=True)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert peak < 50_000


@pytest.mark.parametrize('fraction', ['0' * 100_000 + '1', '0_' * 100_000 + '1'])
def test_nonzero_digits_after_the_fraction_copy_limit_are_not_discarded(fraction: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='not a whole number of nanoseconds'):
		soml.loads(f'[0.{fraction}s]')


@pytest.mark.parametrize('fraction', ['5' + '0' * 100_000, '5_' + '0_' * 100_000 + '0'])
def test_significant_duration_fractions_survive_large_zero_tails(fraction: str) -> None:
	assert soml.loads(f'[0.{fraction}s]') == [timedelta(milliseconds=500)]


def test_invalid_duration_units_still_take_precedence_over_fraction_precision() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='is not a unit'):
		soml.loads('[1.' + '0' * 100_000 + '1unknownunit]')


@pytest.mark.parametrize('quote', ["'", '"'])
def test_newlines_before_a_later_quote_still_terminate_single_line_strings(quote: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Unterminated string'):
		soml.loads(f'[{quote}abc\ndef{quote}]')


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('"plain 😀 text"', 'plain 😀 text'),
		(r'"\nplain\t"', '\nplain\t'),
		(r'"plain\n\t"', 'plain\n\t'),
		(r'"\n\t"', '\n\t'),
	],
)
def test_escaped_string_chunk_boundaries_keep_unicode_and_simple_escapes(text: str, expected: str) -> None:
	assert soml.loads(f'[{text}]') == [expected]


@pytest.mark.parametrize('text', ['0b1_01__01', '0o7__00', '0xAB__CD', '12__34', '1.23__45', '1e12__34'])
def test_bulk_digit_runs_do_not_accept_empty_underscore_groups(text: str) -> None:
	with pytest.raises(soml.SOMLDecodeError, match='underscore'):
		soml.loads(f'[{text}]')


def test_star_heavy_block_comments_preserve_array_separator_rules() -> None:
	source = '[1 /* *** content * * \n **/ , 2 # comma, and stars ***\n,3]'
	with pytest.raises(soml.SOMLDecodeError, match='comma must be on the same line'):
		soml.loads(source)
	assert soml.loads(source.replace('\n,3', '\n3')) == [1, 2, 3]


def test_thousands_separator_diagnostics_do_not_misread_a_long_float_tail() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='Leading zeros') as error_info:
		soml.loads('[1.' + '0' * 100_000 + ',000]')
	assert 'comma separates items' not in error_info.value.msg


def test_comment_masking_preserves_only_the_line_break_outside_a_block_comment() -> None:
	block = '/* first * ***\nsecond **/'
	line = '# line ***'
	source = f' \t{block} , {line}\n'
	assert without_comments(source) == f' \t{" " * len(block)} , {" " * len(line)}\n'
