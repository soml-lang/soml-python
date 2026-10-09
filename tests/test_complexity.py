"""
Parsing and writing must take linear time in the size of the input, whatever its shape. Each case times an input and one eight times its size, and compares the ratio rather than an absolute time, so a slow machine does not fail the test. Linear time gives a ratio near 8 and quadratic time near 64, so the threshold of 24 leaves room for noise. A long run of a repeated regex group, as in `(?:_?[0-9])*`, costs more per character on a large input because its backtracking stack outgrows the CPU caches, which gives ratios up to about 20 while it stays linear.

Each small input is sized to take a few milliseconds, so that fixed costs do not hide a quadratic shape.
"""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import best_time

import soml
from soml import tree

# Only needed for annotations, which are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable

SIZE_FACTOR = 8
MAXIMUM_RATIO = 24


def assert_linear[T](create_input: Callable[[int], T], run: Callable[[T], object], count: int) -> None:
	small = create_input(count)
	large = create_input(count * SIZE_FACTOR)
	ratios: list[float] = []

	# A busy machine can slow one measurement down, so a high ratio is measured again. A quadratic shape is high every time.
	while len(ratios) < 3 and (not ratios or ratios[-1] >= MAXIMUM_RATIO):
		ratios.append(best_time(lambda: run(large), runs=3) / best_time(lambda: run(small), runs=3))

	assert ratios[-1] < MAXIMUM_RATIO, f'{SIZE_FACTOR} times the input took {", ".join(f"{ratio:.1f}" for ratio in ratios)} times as long'


def parse_expecting_error(text: str) -> None:
	try:
		soml.loads(text)
	except soml.SOMLDecodeError:
		return

	raise AssertionError('Expected a SOMLDecodeError')


# Each shape is a function from a count to a document, and the count for the small input.
VALID_SHAPES: dict[str, tuple[Callable[[int], str], int]] = {
	'many short members': (lambda count: '\n'.join(f'k{index}: {index}' for index in range(count)), 2000),
	'many members with comments': (lambda count: '\n'.join(f"k{index}: 'v' # comment" for index in range(count)), 2000),
	'many strings on one line': (lambda count: f'[{"'a', " * count}]', 3_000),
	'many escaped strings on one line': (lambda count: f'[{'"a\\n", ' * count}]', 2_000),
	'many quoted keys on one line': (lambda count: f'{{{", ".join(f"'k{index}': 1" for index in range(count))}}}', 2000),
	'a long literal string': (lambda count: f"a: '{'x' * count}'", 400_000),
	'a long escaped string with many escapes': (lambda count: f'a: "{'x\\n\\u{1f600}\\"\\\\' * count}"', 2_000),
	'a long bare key': (lambda count: f'{"k" * count}: 1', 500_000),
	'a huge float': (lambda count: f'a: 0.{"3" * count}', 80_000),
	'a huge hexadecimal int with leading zeros': (lambda count: f'a: 0x{"0" * count}1', 50_000),
	'a huge int with underscores and leading zeros': (lambda count: f'a: 0b{"0_" * count}1', 30_000),
	'a long block string': (lambda count: f"a: '''\n{'\tline\n' * count}\t'''", 5000),
	'a long escaped block string': (lambda count: f'a: """\n{"\tx\\ty\n" * count}\t"""', 5000),
	'a block string with many blank lines': (lambda count: f"a: '''\n{'\n \t\n' * count}\tx\n{'\n' * count}\ty\n{' \n' * count}\t'''", 2_000),
	'many block strings': (lambda count: f'a: [\n{"\t'''\n\tline\n\t''',\n" * count}]', 1_000),
	'a long line comment': (lambda count: f'# {"x" * count}\na: 1', 1_000_000),
	'a long block comment': (lambda count: f'/* {"x " * count} */\na: 1', 300_000),
	'many block comments': (lambda count: f'[{"/* c */ 1, " * count}]', 2_000),
	'many dates': (lambda count: f'[{"1582-10-10, 0001-01-01, 9999-12-31, " * count}]', 500),
	'many instants and durations': (lambda count: f'[{"2026-09-19T14:00:00.5+01:00, 1h2m3s4ms5us, -90m, 1.25h, " * count}]', 200),
	'a huge duration fraction with trailing zeros': (lambda count: f'a: 1.{"0" * count}s', 40_000),
	'nested arrays side by side': (lambda count: f'[{"[[1]], " * count}]', 1_500),
}

INVALID_SHAPES: dict[str, tuple[Callable[[int], str], int]] = {
	'a huge invalid number': (lambda count: f'a: 1{"x" * count}', 500_000),
	'a huge invalid number of digits and underscores': (lambda count: f'a: {"1_" * count}', 25_000),
	'a huge int out of range': (lambda count: f'a: {"1" * count}', 80_000),
	'a huge hexadecimal int with lowercase digits': (lambda count: f'a: 0x{"1" * count}ff', 50_000),
	'a huge float out of range': (lambda count: f'a: 1{"0" * count}.0', 30_000),
	'a huge float that rounds to zero': (lambda count: f'a: 0.{"0" * count}1', 100_000),
	'a huge invalid date prefix': (lambda count: f'a: 2026-01-01{"0" * count}', 500_000),
	'a huge invalid instant': (lambda count: f'a: 2026-01-01T00:00:00.{"0" * count}Z', 500_000),
	'a huge invalid duration': (lambda count: f'a: 1h{"1m" * count}', 250_000),
	'a huge duration fraction that is not whole nanoseconds': (lambda count: f'a: 0.{"0" * count}1s', 25_000),
	'an error at the end of a long one-line document': (lambda count: f'[{"1, " * count}nope]', 3_000),
	'an error at the end of a long multi-line document': (lambda count: '\n'.join(f'k{index}: 1' for index in range(count)) + '\nnope', 2000),
	'an unterminated literal string': (lambda count: f"a: '{'x' * count}", 400_000),
	'an unterminated escaped string with many escapes': (lambda count: f'a: "{"\\t" * count}', 10_000),
	'an unterminated block string': (lambda count: f"a: '''\n{'\tline\n' * count}", 8_000),
	'an unterminated block comment': (lambda count: f'a: 1 /* {"x" * count}', 1_000_000),
	'a key with spaces on a long line': (lambda count: f'a {"b " * count}', 400_000),
	'a long key with many dots': (lambda count: f'{"a." * count}b: 1', 200_000),
	'a long key before a dot': (lambda count: f'{"a" * count}.b: 1', 400_000),
	'a long key with many dots in braces': (lambda count: f'{{x: 1, {"a." * count}b: 1}}', 200_000),
	'a long bare value document': (lambda count: f"'{'x' * count}'", 200_000),
	'a long run of quotes': (lambda count: f'a: {"'" * count}', 40_000),
}


@pytest.mark.parametrize(('create_input', 'count'), VALID_SHAPES.values(), ids=VALID_SHAPES.keys())
def test_parsing_is_linear(create_input: Callable[[int], str], count: int) -> None:
	soml.loads(create_input(count))
	assert_linear(create_input, soml.loads, count)


@pytest.mark.parametrize(('create_input', 'count'), INVALID_SHAPES.values(), ids=INVALID_SHAPES.keys())
def test_the_error_path_is_linear(create_input: Callable[[int], str], count: int) -> None:
	assert_linear(create_input, parse_expecting_error, count)


def test_invalid_utf8_after_a_long_input_is_linear() -> None:
	assert_linear(lambda count: f"a: '{'é' * count}".encode() + b"\xff'", parse_expecting_error_from_bytes, 200_000)


def parse_expecting_error_from_bytes(data: bytes) -> None:
	try:
		soml.loads(data)
	except soml.SOMLDecodeError:
		return

	raise AssertionError('Expected a SOMLDecodeError')


def deep_value(count: int) -> dict[str, soml.Value]:
	"""`count` levels, each with the same payload, so that a level that copies the levels below it shows."""
	value: dict[str, soml.Value] = {}

	for _ in range(count):
		value = {'a': 'x' * 100_000, 'b': value}

	return value


WRITE_SHAPES: dict[str, tuple[Callable[[int], soml.Document], int]] = {
	'many members': (lambda count: {f'k{index}': index for index in range(count)}, 5000),
	'many array items': (lambda count: [f'item {index}' for index in range(count)], 10_000),
	'many floats, instants, and durations': (
		lambda count: [[index / 7, datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index), timedelta(microseconds=index * 1001)] for index in range(count)],
		250,
	),
	'a long string with escapes': (lambda count: {'a': "it's\n" * count}, 20_000),
	'deep nesting': (deep_value, 12),
}


@pytest.mark.parametrize(('create_input', 'count'), WRITE_SHAPES.values(), ids=WRITE_SHAPES.keys())
def test_writing_is_linear(create_input: Callable[[int], soml.Document], count: int) -> None:
	assert_linear(create_input, soml.dumps, count)


def test_bulk_removal_with_preserved_comments_is_linear() -> None:
	def create_document(count: int) -> tree.DocumentNode:
		document = tree.parse('[\n' + ('# ' + 'x' * 1000 + '\n1\n') * count + ']')
		assert isinstance(document.body, tree.ArrayNode)
		document.body.elements.clear()
		return document

	assert_linear(create_document, tree.unparse, 3000)
