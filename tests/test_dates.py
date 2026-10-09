"""Native calendar dates stay distinct from instants and strings at every API boundary."""

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta, timezone
from typing import assert_type

import pytest

import soml
from soml import tree


@pytest.mark.parametrize('text', ['0001-01-01', '0099-01-01', '9999-12-31', '1582-10-10', '2011-12-30', '2000-02-29', '2024-02-29', '2026-11-01'])
def test_calendar_dates_keep_their_native_type_and_exact_spelling(text: str) -> None:
	expected = date.fromisoformat(text)
	value = soml.loads(f'day: {text}')
	assert isinstance(value, dict)
	assert type(value['day']) is date
	assert value['day'] == expected
	assert soml.dumps({'day': expected}, canonical=True) == f'day: {text}\n'
	assert soml.format(f'day:{text}# calendar') == f'day: {text} # calendar\n'


@pytest.mark.parametrize(
	'text',
	[
		'0000-01-01',
		'10000-01-01',
		'-2026-11-01',
		'+2026-11-01',
		'2026-00-01',
		'2026-13-01',
		'2026-11-00',
		'2026-01-32',
		'1900-02-29',
		'2100-02-29',
		'2026-02-29',
		'2026-02-30',
		'2026-04-31',
		'2026-1-01',
		'2026-11-1',
		'\uff12\uff10\uff12\uff16-11-01',
		'2026-11-01T',
		'2026-11-01Z',
		'2026-11-01foo',
		'2026-11-010',
		'2026-11-01_',
		'2026-11-01+00:00',
		'2026-11-01[u-ca=iso8601]',
		'2026-11-01[UTC]',
		'2026-11-01T12:00:00',
	],
)
def test_invalid_dates_and_date_prefixes_are_rejected_whole(text: str) -> None:
	for parse in [soml.loads, soml.format, tree.parse]:
		with pytest.raises(soml.SOMLDecodeError):
			parse(f'day: {text}')


@pytest.mark.parametrize(
	'source',
	[
		'2026-11-01 12:00:00',
		'2026-11-01  12:00:00',
		'2026-11-01\t12:00:00',
		'2026-11-01 \t 12:00:00',
		'2026-11-01 1:00:00',
		'2026-11-01 9:00',
		'2026-02-29 12:00:00',
	],
)
def test_a_date_followed_by_whitespace_and_a_time_gets_the_separator_diagnostic(source: str) -> None:
	for parse in [soml.loads, soml.format, tree.parse]:
		with pytest.raises(soml.SOMLDecodeError, match='The date and time separator in an instant is an uppercase “T”, not a space') as captured:
			parse(f'day: {source}')

		assert captured.value.colno == 6


def test_a_space_separated_time_shows_the_instant_it_was_meant_to_be() -> None:
	with pytest.raises(soml.SOMLDecodeError, match=r'as in 2026-11-01T12:00:00Z'):
		soml.loads('day: 2026-11-01 12:00:00')

	with pytest.raises(soml.SOMLDecodeError, match=r'as in 2026-11-01T12:00:00\+02:00'):
		soml.loads('day: 2026-11-01\t12:00:00+02:00')


def test_a_time_with_a_one_digit_hour_gets_no_example_instant() -> None:
	with pytest.raises(soml.SOMLDecodeError, match='The date and time separator') as captured:
		soml.loads('day: 2026-11-01 7:32:00')

	assert 'as in' not in captured.value.msg


def test_an_invalid_date_is_reported_at_its_own_position() -> None:
	with pytest.raises(soml.SOMLDecodeError) as captured:
		soml.loads('[\n\t2026-02-30\n]')

	assert (captured.value.lineno, captured.value.colno) == (2, 2)

	with pytest.raises(soml.SOMLDecodeError) as captured:
		soml.loads('a:\n  2026-02-30\n')

	assert (captured.value.lineno, captured.value.colno) == (2, 3)


def test_setting_a_date_or_an_instant_replaces_the_other_type() -> None:
	document = tree.parse('a: 2026-11-01\n')
	document.set('a', datetime(2026, 11, 1, 2, tzinfo=UTC))
	assert tree.unparse(document) == 'a: 2026-11-01T02:00:00Z\n'

	document = tree.parse('a: 2026-11-01T00:00:00Z\n')
	document.set('a', date(2026, 11, 1))
	assert tree.unparse(document) == 'a: 2026-11-01\n'


def test_a_date_can_be_added_as_a_member_or_into_a_missing_object() -> None:
	document = tree.parse('a: 1\n')
	document.set('b', date(2026, 11, 1))
	assert tree.unparse(document) == 'a: 1\nb: 2026-11-01\n'

	document = tree.parse('a: {}\n')
	document.set(('a', 'b'), date(2026, 11, 1))
	assert tree.unparse(document) == 'a: {\n\tb: 2026-11-01\n}\n'


def test_dates_strings_and_instants_remain_distinct() -> None:
	values = soml.loads("[2026-11-01, '2026-11-01', 2026-11-01T00:00:00Z]")
	assert isinstance(values, list)
	assert [type(value) for value in values] == [date, str, datetime]
	assert values == [date(2026, 11, 1), '2026-11-01', datetime(2026, 11, 1, tzinfo=UTC)]
	assert soml.loads(soml.dumps(values)) == values
	instant = datetime(2026, 11, 1, 2, tzinfo=timezone(timedelta(hours=2)))
	assert soml.dumps([instant, date(2026, 11, 1)]) == '[\n\t2026-11-01T00:00:00Z\n\t2026-11-01\n]\n'
	assert isinstance(tree.from_value(instant), tree.InstantNode)


@pytest.mark.parametrize(
	'source',
	['a: 2026-11-01\nb: 2026-11-02', 'a: 2026-11-01# comment\nb: 2026-11-02', 'a: 2026-11-01/* comment */\nb: 2026-11-02', '{a:2026-11-01,b:2026-11-02}', 'a: 2026-11-01\n# next\nb: 2026-11-02'],
)
def test_dates_terminate_at_comments_separators_and_eof(source: str) -> None:
	expected = {'a': date(2026, 11, 1), 'b': date(2026, 11, 2)}
	assert soml.loads(source) == expected
	document = tree.parse(source)
	assert document.to_value() == expected
	assert tree.unparse(document) == source
	assert soml.loads(soml.format(source)) == expected


def test_date_tree_nodes_and_edits_preserve_temporal_types() -> None:
	document = tree.parse("dates: [2000-02-29, '2000-02-29'] # keep\n")
	node = document.get(('dates', 0))
	assert isinstance(node, tree.DateNode)
	assert node.to_value() == date(2000, 2, 29)
	assert [token.kind for token in document.tokens if token.value == '2000-02-29'] == ['date']
	document.set(('dates', 0), date(1582, 10, 10))
	document.set(('dates', 1), date(2011, 12, 30))
	assert tree.unparse(document) == 'dates: [1582-10-10, 2011-12-30] # keep\n'
	document.set(('dates', 0), '1582-10-10')
	assert tree.unparse(document) == "dates: ['1582-10-10', 2011-12-30] # keep\n"
	created = assert_type(tree.from_value(date(1, 1, 1)), tree.DateNode)
	assert type(created.to_value()) is date
	assert tree.unparse(tree.DocumentNode.from_value({'day': date(1, 1, 1)})) == 'day: 0001-01-01\n'


def test_date_nodes_refuse_datetimes_including_naive_ones() -> None:
	document = tree.DocumentNode.from_value({'day': date(2026, 11, 1)})
	node = document.get('day')
	assert isinstance(node, tree.DateNode)

	for value in [datetime(2026, 11, 1), datetime(2026, 11, 1, tzinfo=UTC)]:  # noqa: DTZ001 # A naive datetime must not become a calendar date.
		with pytest.raises(TypeError, match='DateNode holds a date'):
			tree.DateNode(value).to_value()
		node.value = value
		with pytest.raises(TypeError, match='DateNode holds a date'):
			tree.unparse(document)


def test_native_date_subclasses_use_calendar_fields_not_overrides() -> None:
	class CustomDate(date):
		def isoformat(self) -> str:
			return 'wrong'

		def toordinal(self) -> int:
			return 0

	value = CustomDate(1582, 10, 10)
	assert soml.dumps([value]) == '[\n\t1582-10-10\n]\n'
	node = tree.from_value(value)
	assert isinstance(node, tree.DateNode)
	assert type(node.to_value()) is date
	assert node.to_value() == date(1582, 10, 10)


@pytest.mark.parametrize('year', [1, 4, 99, 100, 400, 1582, 1900, 2000, 2100, 9999])
def test_every_month_uses_strict_gregorian_calendar_rules(year: int) -> None:
	for month in range(1, 13):
		last = monthrange(year, month)[1]
		text = f'{year:04}-{month:02}-{last:02}'
		assert soml.loads(f'[{text}]') == [date(year, month, last)]
		with pytest.raises(soml.SOMLDecodeError, match=f'the day must be 01 to {last} in that month'):
			soml.loads(f'[{year:04}-{month:02}-{last + 1:02}]')


def test_untrusted_date_prefixes_have_bounded_diagnostics() -> None:
	with pytest.raises(soml.SOMLDecodeError) as captured:
		soml.loads('[2026-11-01' + '0' * 100_000 + ']')
	assert len(captured.value.msg) < 300
	assert captured.value.colno == 2
