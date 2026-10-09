"""Position validation and compound path normalization boundaries."""

from decimal import Decimal
from enum import IntEnum, StrEnum
from fractions import Fraction
from typing import Any

import pytest

from soml import tree


@pytest.mark.parametrize('offset', [0.5, float('nan'), float('inf'), Decimal(0), Fraction(0), None])
def test_positions_reject_noninteger_offsets(offset: Any) -> None:
	document = tree.parse('a: 1\nb: 2')
	with pytest.raises(TypeError, match='offset must be an int'):
		document.position(offset)


def test_position_integer_subclasses_cannot_replace_comparison_or_arithmetic() -> None:
	class Offset(int):
		def __ge__(self, other: object) -> bool:
			return True

		def __le__(self, other: object) -> bool:
			return True

		def __sub__(self, other: object) -> int:
			raise AssertionError('Normalize the integer before column arithmetic')

	document = tree.parse('a: 1\nb: 2')
	assert document.position(Offset(5)) == (2, 1)
	with pytest.raises(ValueError, match='outside the source'):
		document.position(Offset(-1))
	with pytest.raises(ValueError, match='outside the source'):
		document.position(Offset(100))


def test_compound_paths_normalize_subclasses_without_losing_empty_keys() -> None:
	class Key(StrEnum):
		EMPTY = ''
		ARRAY = 'array'

	class Index(IntEnum):
		ZERO = 0

	document = tree.parse("'': {array: [{value: 1}]}")
	path = (Key.EMPTY, Key.ARRAY, Index.ZERO, 'value')
	node = document.get(path)
	assert isinstance(node, tree.IntegerNode)
	assert node.value == 1
	document.set(path, 2)
	assert document.to_value() == {'': {'array': [{'value': 2}]}}
	assert document.remove(path)
	assert document.to_value() == {'': {'array': [{}]}}


@pytest.mark.parametrize('path', [('a', -1), ('a', True), ('a', 0.0), ('a', '', -1)])
def test_compound_path_validation_checks_every_segment_before_lookup(path: Any) -> None:
	document = tree.parse('unrelated: 1')
	with pytest.raises(TypeError if isinstance(path[-1], bool | float) else ValueError, match='non-empty array'):
		document.get(path)
