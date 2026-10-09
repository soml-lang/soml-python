"""Serialization boundaries for native temporal conversion and sparse Unicode escapes."""

from datetime import UTC, datetime, timedelta, timezone
from enum import IntEnum, StrEnum

import pytest

import soml
from soml import tree


@pytest.mark.parametrize(
	('value', 'expected'),
	[
		(datetime(2024, 3, 1, tzinfo=timezone(timedelta(microseconds=1))), '2024-02-29T23:59:59.999999Z'),
		(datetime(2000, 1, 1, tzinfo=timezone(-timedelta(microseconds=1))), '2000-01-01T00:00:00.000001Z'),
		(datetime(9999, 12, 31, 0, 0, 0, 100000, tzinfo=timezone(timedelta(hours=23))), '9999-12-30T01:00:00.1Z'),
		(datetime(1, 1, 1, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=23, minutes=59, seconds=59, microseconds=999999))), '0001-01-01T00:00:00Z'),
	],
)
def test_instant_normalization_preserves_calendar_carries(value: datetime, expected: str) -> None:
	text = soml.dumps({'instant': value})
	assert text == f'instant: {expected}\n'
	assert soml.loads(text) == {'instant': value}
	assert tree.unparse(tree.DocumentNode.from_value({'instant': value})) == text


@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize('microseconds', [1, 10, 100, 1000, 10000, 100000, 123456, 999999, 9223372036854775])
def test_native_microsecond_duration_arithmetic_is_exact(sign: int, microseconds: int) -> None:
	value = timedelta(microseconds=sign * microseconds)
	text = soml.dumps({'duration': value})
	assert soml.loads(text) == {'duration': value}
	assert tree.unparse(tree.DocumentNode.from_value({'duration': value})) == text
	assert text.endswith('s\n')
	assert '0000000' not in text.partition('.')[2]


@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize(('microseconds', 'fraction'), [(100001, '100001'), (10010, '01001'), (100100, '1001'), (1010, '00101'), (101010, '10101'), (101000, '101')])
def test_duration_fraction_padding_keeps_internal_zeros(sign: int, microseconds: int, fraction: str) -> None:
	value = sign * timedelta(hours=1, minutes=2, seconds=3, microseconds=microseconds)
	expected = f'duration: {"-" if sign < 0 else ""}1h2m3.{fraction}s\n'

	assert soml.dumps({'duration': value}) == expected
	assert tree.unparse(tree.DocumentNode.from_value({'duration': value})) == expected
	assert soml.loads(expected) == {'duration': value}


@pytest.mark.parametrize('sign', [-1, 1])
def test_first_unrepresentable_whole_microsecond_is_rejected(sign: int) -> None:
	value = timedelta(microseconds=sign * 9223372036854776)
	with pytest.raises(ValueError, match='64-bit range of nanoseconds'):
		soml.dumps({'duration': value})
	with pytest.raises(ValueError, match='64-bit range of nanoseconds'):
		tree.from_value(value)


@pytest.mark.parametrize('prefix', ['日本語', '😀', 'e\u0301', '\u0085', '\U0010ffff'])
def test_unicode_escaping_keeps_every_unescaped_code_point(prefix: str) -> None:
	value = prefix * 1000 + '\n\t"\\\x00\x1f\x7f' + prefix
	text = soml.dumps({value: value})
	assert soml.loads(text) == {value: value}
	assert '\\u{0}' in text
	assert '\\u{1f}' in text
	assert '\\u{7f}' in text
	assert prefix * 1000 in text
	assert tree.unparse(tree.DocumentNode.from_value({value: value})) == text


def test_scalar_subclasses_keep_their_values_in_nested_collections() -> None:
	class Label(StrEnum):
		VALUE = "it's 日本語"

	class Code(IntEnum):
		VALUE = 1729

	value: dict[str, soml.Serializable] = {'nested': [{Label.VALUE: (Code.VALUE, Label.VALUE)}]}
	assert soml.loads(soml.dumps(value)) == {'nested': [{"it's 日本語": [1729, "it's 日本語"]}]}
	assert soml.dumps(soml.loads(soml.dumps(value)), canonical=True) == soml.dumps(value, canonical=True)


def test_native_instant_formatting_preserves_the_timezone_free_error() -> None:
	with pytest.raises(ValueError, match='has no time zone'):
		soml.dumps([datetime(2026, 1, 1)])  # noqa: DTZ001 # An invalid instant, on purpose.


def test_non_ascii_surrogates_are_still_rejected_before_carriage_returns() -> None:
	with pytest.raises(ValueError, match='lone surrogate'):
		soml.dumps({'value': '日本語\r\ud800'})
	with pytest.raises(ValueError, match='carriage return'):
		soml.dumps({'value': '日本語\r'})


def test_utc_year_padding_is_independent_of_the_platform_strftime() -> None:
	for year in (1, 9, 99, 999, 9999):
		value = datetime(year, 1, 1, tzinfo=UTC)
		assert soml.dumps([value]) == f'[\n\t{year:04}-01-01T00:00:00Z\n]\n'
