"""Conversions preserve exact values across writer and tree APIs."""

import math
from datetime import UTC, datetime, timedelta
from typing import Self
from zoneinfo import ZoneInfo

import pytest

import soml
from soml import tree


@pytest.mark.parametrize('value', ['', 'plain', 'é😀', "quote' and newline\n\t", 'backslash\\'])
def test_string_subclasses_are_normalized_without_calling_display_overrides(value: str) -> None:
	class Text(str):
		__slots__ = ()

		def __str__(self) -> str:
			raise AssertionError('Use the underlying string value')

		def __format__(self, specification: str) -> str:
			raise AssertionError('Use the underlying string value')

	text = Text(value)
	assert soml.loads(soml.dumps({text: text})) == {value: value}
	node = tree.from_value(text)
	assert node.to_value() == value
	assert type(node.to_value()) is str
	assert tree.DocumentNode.from_value({text: text}).to_value() == {value: value}


@pytest.mark.parametrize('microseconds', [-(2**63 // 1000) - 1, -(2**63 // 1000), -1, 0, 1, 2**63 // 1000, 2**63 // 1000 + 1])
def test_duration_subclasses_keep_exact_precision_and_range_checks(microseconds: int) -> None:
	class Duration(timedelta):
		def __pos__(self) -> Self:
			raise AssertionError('Use the base timedelta operation')

	duration = Duration(microseconds=microseconds)
	if abs(microseconds) > 2**63 // 1000:
		with pytest.raises(ValueError, match='outside the 64-bit range of nanoseconds'):
			tree.from_value(duration)
		with pytest.raises(ValueError, match='outside the 64-bit range of nanoseconds'):
			tree.DurationNode(duration).to_value()
		return

	expected = timedelta(microseconds=microseconds)
	for node in (tree.from_value(duration), tree.DurationNode(duration)):
		assert node.to_value() == expected
		assert type(node.to_value()) is timedelta


@pytest.mark.parametrize('value', [-(2**63), -1, 0, 1, 2**63 - 1])
def test_integer_subclasses_are_normalized_without_calling_conversion_overrides(value: int) -> None:
	class Integer(int):
		def __int__(self) -> int:
			raise AssertionError('Use the underlying integer value')

		def __index__(self) -> int:
			raise AssertionError('Use the underlying integer value')

		def __float__(self) -> float:
			raise AssertionError('Normalize the integer before converting to float')

	integer = Integer(value)
	assert soml.loads(soml.dumps([integer])) == [value]
	node = tree.from_value(integer)
	assert node.to_value() == value
	assert type(node.to_value()) is int
	assert tree.FloatNode(integer).to_value() == float(value)


def test_index_protocol_alone_does_not_make_an_object_serializable() -> None:
	class Indexable:
		def __index__(self) -> int:
			raise AssertionError('Only int instances are supported')

	value = Indexable()
	with pytest.raises(TypeError, match='not SOML serializable'):
		soml.dumps([value])  # type: ignore[list-item]
	with pytest.raises(TypeError, match='not SOML serializable'):
		tree.from_value(value)  # type: ignore[call-overload]
	with pytest.raises(TypeError, match='holds an int'):
		tree.IntegerNode(value).to_value()  # type: ignore[arg-type]


@pytest.mark.parametrize('fold', [0, 1])
def test_ambiguous_local_instants_preserve_the_selected_occurrence(fold: int) -> None:
	instant = datetime(2026, 11, 1, 1, 30, 0, 123456, tzinfo=ZoneInfo('America/New_York'), fold=fold)
	expected = datetime(2026, 11, 1, 5 + fold, 30, 0, 123456, tzinfo=UTC)
	document = tree.DocumentNode.from_value({'instant': instant})

	assert soml.loads(soml.dumps({'instant': instant})) == {'instant': expected}
	assert document.to_value() == {'instant': expected}
	assert soml.loads(tree.unparse(document)) == {'instant': expected}


def test_temporal_subclasses_are_normalized_without_calling_their_constructors() -> None:
	class Instant(datetime):
		def __add__(self, other: object) -> Self:
			raise AssertionError('Use the datetime value')

	class Duration(timedelta):
		@property
		def days(self) -> int:
			raise AssertionError('Use the timedelta value')

	instant = Instant(2026, 1, 1, tzinfo=UTC)
	duration = Duration(microseconds=-1)
	document = tree.DocumentNode.from_value({'instant': instant, 'duration': duration})
	value = document.to_value()

	assert isinstance(value, dict)
	assert type(value['instant']) is datetime
	assert type(value['duration']) is timedelta
	assert soml.loads(tree.unparse(document)) == {'instant': datetime(2026, 1, 1, tzinfo=UTC), 'duration': timedelta(microseconds=-1)}


@pytest.mark.parametrize('canonical', [False, True])
def test_shared_nested_containers_preserve_layout_and_scalar_types(*, canonical: bool) -> None:
	shared: dict[str, soml.Serializable] = {'z': [], 'a': {}, 'value': (1, True, None)}
	value: list[soml.Serializable] = [shared, shared, {}, [], ()]
	text = soml.dumps(value, canonical=canonical)

	assert soml.loads(text) == [{'z': [], 'a': {}, 'value': [1, True, None]}] * 2 + [{}, [], []]
	assert text.count('\t\tz: []\n') == 2
	assert text.count('\t\ta: {}\n') == 2
	assert text.endswith('\t[]\n]\n')
	assert soml.dumps(soml.loads(text), canonical=canonical) == text


def test_float_neighbors_across_decimal_powers_round_trip_with_canonical_layout() -> None:
	values = [neighbor * sign for exponent in range(-323, 309) for neighbor in (10.0**exponent, math.nextafter(10.0**exponent, 0.0), math.nextafter(10.0**exponent, math.inf)) for sign in (1, -1)]

	for value in values:
		text = soml.dumps([value])
		result = soml.loads(text)
		assert result == [value]
		assert isinstance(result, list)
		assert type(result[0]) is float
		assert ('e' in text) == (abs(value) < 1e-6 or abs(value) >= 1e21)
		assert soml.dumps(result) == text


@pytest.mark.parametrize('truth', [False, True])
def test_container_emptiness_uses_length_instead_of_custom_truthiness(*, truth: bool) -> None:
	class Mapping(dict[str, soml.Serializable]):
		def __bool__(self) -> bool:
			return truth

	class Sequence(list[soml.Serializable]):
		def __bool__(self) -> bool:
			return truth

	assert soml.dumps({'mapping': Mapping(a=1), 'sequence': Sequence([1])}) == 'mapping: {\n\ta: 1\n}\nsequence: [\n\t1\n]\n'
	assert soml.dumps({'mapping': Mapping(), 'sequence': Sequence()}) == 'mapping: {}\nsequence: []\n'
