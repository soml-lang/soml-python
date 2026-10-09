"""
Property tests with hypothesis: random values survive a round trip, random legal spellings of a value read back as that value, floats are laid out as canonical form rule 11 says, and random or mutated input only ever raises `SOMLDecodeError`.

The example counts come from the hypothesis profile in `conftest.py`. Run with `HYPOTHESIS_PROFILE=thorough` for a long search.
"""

import math
import re
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from random import Random  # noqa: TC003 # Hypothesis reads the annotations of a test at runtime.

import pytest
from helpers import value_paths
from hypothesis import given, note, settings
from hypothesis import strategies as st

import soml
from soml import tree

# Only needed for annotations, which are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
MAX_DEPTH = 100
MICROSECOND = timedelta(microseconds=1)
# The largest duration in whole microseconds whose nanoseconds fit in int64.
MAX_DURATION = timedelta(microseconds=INT64_MAX // 1000)

DURATION_UNITS = [
	('h', 3_600_000_000_000),
	('m', 60_000_000_000),
	('s', 1_000_000_000),
	('ms', 1_000_000),
	('us', 1000),
	('ns', 1),
]

BARE_KEY = re.compile(r'[A-Za-z0-9_-]+')
# What a `'...'` string cannot hold: its quote, a line feed, and a control character other than tab.
NOT_LITERAL = re.compile("[\x00-\x08\x0a-\x1f\x7f']")
# What a `'''` block cannot hold: a control character other than tab and line feed.
NOT_LITERAL_BLOCK = re.compile('[\x00-\x08\x0b-\x1f\x7f]')
CONTROL = re.compile('[\x00-\x08\x0a-\x1f\x7f]')

# Characters that reach every branch of string and key writing and reading, as in the JS reference's round-trip test.
INTERESTING_CHARACTERS = [
	'a',
	'Z',
	'0',
	'_',
	'-',
	'.',
	' ',
	'  ',
	'\t',
	'\n',
	'\n\n',
	"'",
	'"',
	'\\',
	"'''",
	'"""',
	"''''",
	'#',
	'/*',
	'*/',
	':',
	',',
	'{',
	']',
	'\x00',
	'\x01',
	'\x1f',
	'\x7f',
	'\x80',
	'\xa0',
	'é',
	'\u2028',
	'\ufeff',
	'\ufffd',
	'\uffff',
	'😀',
	'\U0010ffff',
]

COMMENT_CHARACTERS = list('abc #/*\'"{}[],:\t\\é😀\u2028\ufeff')

# Characters and tokens for fuzzing, chosen to reach the parser's states and its error paths.
FUZZ_TOKENS = [
	*'{}[]:,.\'"\\#/*-_0123456789eTZ \t\n\r',
	'\xa0',
	'\u2028',
	'\ufeff',
	'é',
	'😀',
	'\x00',
	'\x7f',
	'\ud800',
	'x',
	'h',
	'ms',
	'ns',
	'0x',
	'0o',
	'0b',
	'1_0',
	'1.5',
	'1e5',
	'infinity',
	'-infinity',
	'true',
	'null',
	"'''\n",
	'"""\n',
	'\\u{',
	'\\u{1f600}',
	'/* c */',
	'# c\n',
	'1582-10-10',
	'2026-01-01T00:00:00Z',
	'+01:00',
	'1h30m',
]

# Byte sequences that are not valid UTF-8, or a BOM, which is valid UTF-8 but not a valid document start.
FUZZ_BYTES = [b'\xff', b'\x80', b'\xc0\x80', b'\xed\xa0\x80', b'\xe2\x82', b'\xf4\x90\x80\x80', b'\xef\xbb\xbf', b'\r', b'\x00']

VALID_DOCUMENTS = sorted(path.read_bytes().decode() for path in (Path(__file__).parent / 'conformance' / 'valid').rglob('*.soml'))


def scaled(factor: float) -> settings:
	"""Settings with `factor` times the profile's example count, for a test that is cheaper or costlier than most."""
	return settings(max_examples=round(settings().max_examples * factor))


def typed(value: object) -> object:
	"""Pairs every scalar with its type, because `1 == 1.0 == True` in Python. A float is compared by `repr()`, so that the sign of a zero shows, and a datetime by `isoformat()`, so that its offset shows."""
	if isinstance(value, list):
		return [typed(item) for item in value]

	if isinstance(value, dict):
		return {key: typed(member) for key, member in value.items()}

	if isinstance(value, float):
		return ('float', repr(value))

	if isinstance(value, datetime):
		return ('datetime', value.isoformat())

	return (type(value).__name__, value)


def container_depth(value: object) -> int:
	if isinstance(value, list):
		return 1 + max((container_depth(item) for item in value), default=0)

	if isinstance(value, dict):
		return 1 + max((container_depth(member) for member in value.values()), default=0)

	return 0


# Strategies

characters = st.one_of(st.sampled_from(INTERESTING_CHARACTERS), st.characters(exclude_categories=['Cs'], exclude_characters='\r'))
strings = st.lists(characters, max_size=12).map(''.join)
keys = st.one_of(
	st.from_regex(r'[A-Za-z0-9_-]{1,8}', fullmatch=True),
	st.sampled_from(['', '404', '007', 'true', 'null', 'infinity', '-foo', '2024-01-01', 'a.b', 'the name']),
	strings,
)
# Negative zero reads back as zero, so the round trip starts from zero. It is written as `-0.0` by the respelling, and has its own test.
floats = st.floats(allow_nan=False).map(lambda value: value + 0.0)
integers = st.one_of(st.integers(INT64_MIN, INT64_MAX), st.integers(-1000, 1000), st.sampled_from([INT64_MIN, INT64_MAX, 0, -1, 2**53 + 1]))
instants = st.one_of(
	st.datetimes(timezones=st.just(UTC)),
	st.datetimes(timezones=st.just(UTC)).map(lambda value: value.replace(microsecond=0)),
	st.sampled_from([datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)]),
)
durations = st.one_of(
	st.timedeltas(min_value=-MAX_DURATION, max_value=MAX_DURATION),
	st.integers(-(9 * 10**9), 9 * 10**9).map(lambda seconds: timedelta(seconds=seconds)),
	st.integers(-(10**7), 10**7).map(lambda microseconds: timedelta(microseconds=microseconds)),
	st.sampled_from([timedelta(0), MAX_DURATION, -MAX_DURATION, timedelta(minutes=90), timedelta(hours=1, microseconds=1)]),
)
scalars: st.SearchStrategy[soml.Value] = st.one_of(strings, integers, floats, st.booleans(), st.none(), instants, st.dates(), durations)


def extend(children: st.SearchStrategy[soml.Value]) -> st.SearchStrategy[soml.Value]:
	return st.one_of(st.lists(children, max_size=4), st.dictionaries(keys, children, max_size=4))


values: st.SearchStrategy[soml.Value] = st.recursive(scalars, extend, max_leaves=24)
documents: st.SearchStrategy[soml.Document] = st.one_of(st.dictionaries(keys, values, max_size=6), st.lists(values, max_size=6))


# Respelling


def leading_quote_run(line: str, quote: str) -> int:
	"""The length of the run of `quote` that a line starts with after its spaces and tabs, which closes a block string when it is as long as the delimiter."""
	content = line.lstrip(' \t')
	return len(content) - len(content.lstrip(quote))


def is_blank(line: str) -> bool:
	return not line.strip(' \t')


def unicode_escape(character: str) -> str:
	return f'\\u{{{ord(character):x}}}'


def terminating_decimal(fraction: Fraction) -> str | None:
	"""The digits after the point of a fraction below 1, or `None` when its decimal does not terminate."""
	denominator = fraction.denominator
	places = 0

	while denominator % 10 == 0:
		denominator //= 10
		places += 1

	twos = fives = 0

	while denominator % 2 == 0:
		denominator //= 2
		twos += 1

	while denominator % 5 == 0:
		denominator //= 5
		fives += 1

	if denominator != 1:
		return None

	places += max(twos, fives)
	return str(fraction.numerator * 10**places // fraction.denominator).rjust(places, '0')


class Respeller:
	"""
	Writes a value in random legal spellings that canonical form never uses: quoted keys, block strings, escapes, radix ints, underscores, exponents, offsets, durations in other units, comments, and whitespace. Every choice follows the spec, so the text must read back as the same value.
	"""

	def __init__(self, random: Random) -> None:
		self.random = random

	def chance(self, probability: float) -> bool:
		return self.random.random() < probability

	def document(self, value: soml.Document) -> str:
		if isinstance(value, dict) and value and self.chance(0.6):
			return self.bare_object(value)

		return f'{self.space()}{self.value(value)}{self.space()}'

	def value(self, value: soml.Value) -> str:
		# `bool` is a subclass of `int`, so it comes first.
		if isinstance(value, bool):
			return 'true' if value else 'false'

		if value is None:
			return 'null'

		if isinstance(value, str):
			return self.string(value)

		if isinstance(value, int):
			return self.integer(value)

		if isinstance(value, float):
			return self.float_value(value)

		if isinstance(value, datetime):
			return self.instant(value)

		if isinstance(value, date):
			return date.isoformat(value)

		if isinstance(value, timedelta):
			return self.duration(value)

		if isinstance(value, list):
			return self.array(value)

		return self.braced_object(value)

	# Whitespace and comments

	def space(self, *, line_breaks: bool = True) -> str:
		"""Whitespace and comments. Without line breaks it is the grammar's `sp`, where only a block comment may hold a line break."""
		parts: list[str] = []

		for _ in range(self.random.choice([0, 0, 1, 1, 2, 3])):
			match self.random.randrange(5 if line_breaks else 3):
				case 0:
					parts.append(' ' * self.random.randint(1, 3))
				case 1:
					parts.append('\t')
				case 2:
					parts.append(self.block_comment())
				case 3:
					parts.append('\n')
				case _:
					parts.append(f'#{self.comment_text()}\n')

		return ''.join(parts)

	def comment_text(self, *, line_breaks: bool = False) -> str:
		alphabet = [*COMMENT_CHARACTERS, '\n'] if line_breaks else COMMENT_CHARACTERS
		return ''.join(self.random.choice(alphabet) for _ in range(self.random.randint(0, 8)))

	def block_comment(self) -> str:
		body = self.comment_text(line_breaks=True)

		# The body may not hold `/*` or `*/`. Splitting one can make another, so it repeats until there are none.
		while '/*' in body or '*/' in body:
			body = body.replace('/*', '/ *').replace('*/', '* /')

		return f'/* {body} */' if body else '/**/'

	def line_separator(self) -> str:
		"""What separates two entries of a brace-less object: at least one line break, outside a comment."""
		line_comment = f'#{self.comment_text()}' if self.chance(0.3) else ''
		return f'{self.space(line_breaks=False)}{line_comment}\n{self.space()}'

	def item_separator(self) -> str:
		"""What separates two items inside brackets: a comma, a line break outside a comment, or both. A comma must be on the line of the item before it."""
		return f'{self.space(line_breaks=False)},{self.space()}' if self.chance(0.6) else self.line_separator()

	# Containers

	def array(self, items: list[soml.Value]) -> str:
		parts = ['[', self.space()]

		for index, item in enumerate(items):
			if index > 0:
				parts.append(self.item_separator())

			parts.append(self.value(item))

		# A trailing comma is only allowed after an item.
		if items and self.chance(0.5):
			parts.append(f'{self.space(line_breaks=False)},')

		parts.append(f'{self.space()}]')
		return ''.join(parts)

	def braced_object(self, members: dict[str, soml.Value]) -> str:
		entries = self.entries(members)
		parts = ['{', self.space()]

		for index, entry in enumerate(entries):
			if index > 0:
				parts.append(self.item_separator())

			parts.append(entry)

		if entries and self.chance(0.5):
			parts.append(f'{self.space(line_breaks=False)},')

		parts.append(f'{self.space()}}}')
		return ''.join(parts)

	def bare_object(self, members: dict[str, soml.Value]) -> str:
		return f'{self.space()}{self.line_separator().join(self.entries(members))}{self.space()}'

	def entries(self, members: dict[str, soml.Value]) -> list[str]:
		"""The `key: value` entries of an object, in random order. Nothing may come between a key and its `:`, but whitespace, line breaks included, and comments may follow it."""
		entries = [f'{self.key(key)}:{self.space()}{self.value(member)}' for key, member in members.items()]
		self.random.shuffle(entries)
		return entries

	def key(self, key: str) -> str:
		options = [self.escaped_string(key)]

		if BARE_KEY.fullmatch(key) is not None:
			options.append(key)

		if NOT_LITERAL.search(key) is None:
			options.append(f"'{key}'")

		return self.random.choice(options)

	# Strings

	def string(self, text: str) -> str:
		options = [self.escaped_string(text)]

		if NOT_LITERAL.search(text) is None:
			options.append(f"'{text}'")

		for quote in ("'", '"'):
			block = self.block_string(text, quote)

			if block is not None:
				options.append(block)

		return self.random.choice(options)

	def escaped_string(self, text: str) -> str:
		return f'"{self.escape(text, is_block=False)}"'

	def escape(self, text: str, *, is_block: bool) -> str:
		"""Escapes what must be escaped, and some other characters at random. In a block string, a `"` may stay as it is, and a line feed is never passed in."""
		parts: list[str] = []

		for character in text:
			if character == '\\':
				parts.append('\\\\' if self.chance(0.8) else unicode_escape(character))
			elif character == '"' and (not is_block or self.chance(0.3)):
				parts.append('\\"' if self.chance(0.8) else unicode_escape(character))
			elif character == '\n':
				parts.append('\\n' if self.chance(0.8) else unicode_escape(character))
			elif character == '\t':
				parts.append(self.random.choice(['\t', '\\t', unicode_escape(character)]))
			elif CONTROL.fullmatch(character) is not None or self.chance(0.1):
				parts.append(unicode_escape(character))
			else:
				parts.append(character)

		return ''.join(parts)

	def block_string(self, text: str, quote: str) -> str | None:
		"""A literal or escaped block string with random indentation and blank lines, or `None` when the text cannot be written as that kind of block."""
		lines = text.split('\n') if text else []

		if quote == "'":
			if NOT_LITERAL_BLOCK.search(text) is not None:
				return None

			# Blank lines at the start and end are dropped, and a blank line in between becomes empty.
			if lines and (is_blank(lines[0]) or is_blank(lines[-1]) or any(line and is_blank(line) for line in lines)):
				return None
		else:
			lines = self.escaped_block_lines(lines)

		# A line that starts with a run of exactly as many quotes as the delimiter would close the block, so a longer delimiter is used.
		delimiter_length = 3 + self.random.choice([0, 0, 0, 1, 2])

		while any(leading_quote_run(line, quote) == delimiter_length for line in lines):
			delimiter_length += 1

		delimiter = quote * delimiter_length
		indentation = self.whitespace()
		body = [self.whitespace() for _ in range(self.random.choice([0, 0, 1, 2]))]
		body.extend(f'{indentation}{line}' if line else self.whitespace() for line in lines)
		body.extend(self.whitespace() for _ in range(self.random.choice([0, 0, 1, 2])))
		return f'{delimiter}\n{"".join(f"{line}\n" for line in body)}{indentation}{delimiter}'

	def escaped_block_lines(self, lines: list[str]) -> list[str]:
		"""The source lines of an escaped block string, which can hold any string, because a line feed or a space can be written as an escape."""
		source_lines: list[str] = []

		for line in lines:
			escaped = self.escape(line, is_block=True)

			# Some line feeds are written as escapes, which joins two lines.
			if source_lines and self.chance(0.2):
				source_lines[-1] += f'\\n{escaped}'
			else:
				source_lines.append(escaped)

		# A line of only spaces and tabs would read as an empty line, so its first character is escaped.
		source_lines = [unicode_escape(line[0]) + line[1:] if line and is_blank(line) else line for line in source_lines]

		# An empty line at the start or the end would be dropped, so it is joined to its neighbour with an escaped line feed.
		while len(source_lines) > 1 and not source_lines[0]:
			source_lines[0:2] = [f'\\n{source_lines[1]}']

		while len(source_lines) > 1 and not source_lines[-1]:
			source_lines[-2:] = [f'{source_lines[-2]}\\n']

		return source_lines

	def whitespace(self) -> str:
		return ''.join(self.random.choice(' \t') for _ in range(self.random.randint(0, 3)))

	# Numbers

	def underscores(self, digits: str) -> str:
		"""Puts a `_` between some pairs of digits."""
		return ''.join(f'_{digit}' if index > 0 and self.chance(0.2) else digit for index, digit in enumerate(digits))

	def integer(self, value: int) -> str:
		# An alternate radix takes no sign, and allows leading zeros.
		match 0 if value < 0 else self.random.randrange(4):
			case 0:
				return f'{"-" if value < 0 else ""}{self.underscores(str(abs(value)))}'
			case 1:
				return f'0x{self.underscores("0" * self.random.randint(0, 3) + f"{value:X}")}'
			case 2:
				return f'0o{self.underscores("0" * self.random.randint(0, 3) + f"{value:o}")}'
			case _:
				return f'0b{self.underscores("0" * self.random.randint(0, 3) + f"{value:b}")}'

	def float_value(self, value: float) -> str:
		if math.isinf(value):
			return 'infinity' if value > 0 else '-infinity'

		if value == 0:
			return self.random.choice(['0.0', '-0.0', '0e0', '-0e0', '0.000', '0.0e-5', '0e7', '0.0_0'])

		sign = '-' if value < 0 else ''
		# The shortest digits that read back as the value, or the exact value of the binary64. Both are a decimal that rounds to the value.
		decimal = Decimal(repr(abs(value))) if self.chance(0.5) else Decimal(abs(value))
		_, digit_tuple, exponent = decimal.as_tuple()
		assert isinstance(exponent, int)
		digits = ''.join(map(str, digit_tuple)).lstrip('0')
		# The value is int(digits) times 10 to the power of `exponent`, and it is written as a mantissa times 10 to the power of `written_exponent`.
		written_exponent = self.random.randint(exponent - 3, exponent + len(digits) + 3)
		shift = exponent - written_exponent

		if shift >= 0:
			integer_part, fraction = digits + '0' * shift, ''
		elif -shift < len(digits):
			integer_part, fraction = digits[:shift], digits[shift:]
		else:
			integer_part, fraction = '0', '0' * (-shift - len(digits)) + digits

		if fraction:
			fraction += '0' * self.random.choice([0, 0, 1, 3])

		# A float needs a fraction or an exponent, and the exponent zero is written `e0`.
		has_exponent = written_exponent != 0 or not fraction or self.chance(0.3)
		exponent_text = f'e{"-" if written_exponent < 0 else ""}{self.underscores(str(abs(written_exponent)))}' if has_exponent else ''
		fraction_text = f'.{self.underscores(fraction)}' if fraction else ''
		return f'{sign}{self.underscores(integer_part)}{fraction_text}{exponent_text}'

	# Instants and durations

	def instant(self, value: datetime) -> str:
		offset_minutes = 0 if self.chance(0.3) else self.random.randint(-(23 * 60 + 59), 23 * 60 + 59)

		try:
			local = value + timedelta(minutes=offset_minutes)
		except OverflowError:
			# The offset would move the local time out of the years 0001 to 9999, which an instant cannot be written in.
			offset_minutes = 0
			local = value

		if offset_minutes == 0:
			# `-00:00` means "offset unknown", which is an error.
			offset = self.random.choice(['Z', '+00:00'])
		else:
			hours, minutes = divmod(abs(offset_minutes), 60)
			offset = f'{"-" if offset_minutes < 0 else "+"}{hours:02}:{minutes:02}'

		if local.microsecond:
			digits = f'{local.microsecond:06}'.rstrip('0')
			fraction = f'.{digits}{"0" * self.random.randint(0, 9 - len(digits))}'
		else:
			fraction = self.random.choice(['', '', '.0', '.000000000'])

		return f'{local.year:04}-{local.month:02}-{local.day:02}T{local.hour:02}:{local.minute:02}:{local.second:02}{fraction}{offset}'

	def duration(self, value: timedelta) -> str:
		nanoseconds = value // MICROSECOND * 1000
		remaining = abs(nanoseconds)
		units = [DURATION_UNITS[index] for index in sorted(self.random.sample(range(len(DURATION_UNITS)), self.random.randint(1, 3)))]
		parts = ['-' if nanoseconds < 0 else '']

		# A part has no cap, so `90m` is as valid as `1h30m`.
		for unit, size in units[:-1]:
			count = remaining // size if self.chance(0.7) else self.random.randint(0, remaining // size)
			parts.append(f'{self.underscores(str(count))}{unit}')
			remaining -= count * size

		unit, size = units[-1]
		whole, rest = divmod(remaining, size)
		parts.append(self.underscores(str(whole)))
		# Only the last part may have a fraction, and only when its decimal is exact. Otherwise the rest is a part of its own in nanoseconds.
		fraction = terminating_decimal(Fraction(rest, size)) if rest else ''

		if fraction is None:
			parts.append(f'{unit}{self.underscores(str(rest))}ns')
		else:
			if fraction or self.chance(0.2):
				fraction = f'{fraction or "0"}{"0" * self.random.choice([0, 0, 2])}'
				parts.append(f'.{self.underscores(fraction)}')

			parts.append(unit)

		return ''.join(parts)


# Round trip


@given(documents)
def test_round_trip_and_fixed_point(value: soml.Document) -> None:
	text = soml.dumps(value)
	note(text)
	read = soml.loads(text)
	assert typed(read) == typed(value)
	assert soml.dumps(read) == text
	canonical = soml.dumps(value, canonical=True)
	assert soml.dumps(soml.loads(canonical), canonical=True) == canonical


@given(keys, scalars)
@scaled(3)
def test_scalars_round_trip_as_values_and_strings_as_keys(key: str, scalar: soml.Value) -> None:
	value = {key: scalar}
	assert typed(soml.loads(soml.dumps(value))) == typed(value)


def test_negative_zero_reads_back_as_zero() -> None:
	text = soml.dumps({'a': -0.0})
	assert text == 'a: 0.0\n'
	assert typed(soml.loads(text)) == {'a': ('float', '0.0')}


# Respelling


@given(documents, st.randoms(use_true_random=False))
def test_respelled_documents_read_back_as_the_same_value(value: soml.Document, random: Random) -> None:
	text = Respeller(random).document(value)
	note(text)
	assert typed(soml.loads(text)) == typed(value)


@given(values, st.randoms(use_true_random=False))
def test_respelled_values_read_back_as_the_same_value(value: soml.Value, random: Random) -> None:
	respeller = Respeller(random)
	text = f'a:{respeller.space()}{respeller.value(value)}{respeller.space()}'
	note(text)
	assert typed(soml.loads(text)) == typed({'a': value})


@given(documents, st.data())
def test_an_edit_to_a_formatted_document_is_already_formatted(value: soml.Document, data: st.DataObject) -> None:
	"""An edit keeps the layout of everything it does not change, so an edit to a formatted document gives a formatted document, as the editing rules of the spec say."""
	text = soml.dumps(value)
	paths = [*value_paths(value), *(('new',) if isinstance(value, dict) else (len(value),))]
	path = data.draw(st.sampled_from(paths))
	document = tree.parse(text)

	if data.draw(st.booleans()):
		document.set(path, data.draw(scalars))
	else:
		document.remove(path)

	edited = tree.unparse(document)
	note(edited)
	assert soml.format(edited) == edited


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		("a: '''\n\t  x\n\n \t\n\t  ''''\n\t  '''", "x\n\n\n''''"),
		('a: """\n\t\\u{20}\n\t"""', ' '),
		('a: """\n\t\\n\n\t"""', '\n'),
		("a: ''''\n'''\n''''", "'''"),
		('a: 1h1.5m', timedelta(hours=1, seconds=90)),
		('a: 0x0_0F', 15),
		('a: 1_0.0_1e-1_0', 10.01e-10),
		('a: 0001-01-01T00:59:00+00:59', datetime(1, 1, 1, 0, 0, tzinfo=UTC)),
	],
)
def test_respelling_examples(text: str, expected: soml.Value) -> None:
	"""Spellings that the respelling makes rarely, checked once by hand."""
	assert typed(soml.loads(text)) == typed({'a': expected})


# Float layout


def shortest_digits(value: float) -> tuple[str, int]:
	"""
	Step 2 of canonical form rule 11, computed with exact fractions: the fewest digits `s` for which `s` times 10 to the power of `n - k` reads back as `value`, the closest to it among those, and the even one on a tie. Returns the digits and `n`.
	"""
	exact = Fraction(value)
	n = math.floor(math.log10(value)) + 1

	# The logarithm can be off by one near a power of ten, so the estimate is corrected with exact arithmetic.
	while exact >= Fraction(10) ** n:
		n += 1

	while exact < Fraction(10) ** (n - 1):
		n -= 1

	for k in range(1, 18):
		scale = Fraction(10) ** (n - k)
		low = math.floor(exact / scale)
		candidates = [digits for digits in (low, low + 1) if reads_back_as(digits * scale, value)]

		if candidates:
			best = min(candidates, key=lambda digits: (abs(digits * scale - exact), digits % 2))
			# Rounding up to the next power of ten, which has one digit.
			return ('1', n + 1) if best == 10**k else (str(best), n)

	raise AssertionError(f'No digits read back as {value!r}')


def reads_back_as(number: Fraction, value: float) -> bool:
	try:
		return float(number) == value
	except OverflowError:
		return False


def reference_float_text(value: float) -> str:
	"""Canonical form rule 11, laid out from `shortest_digits()` and not from `repr()`."""
	if math.isinf(value):
		return 'infinity' if value > 0 else '-infinity'

	if value == 0:
		return '0.0'

	if value < 0:
		return f'-{reference_float_text(-value)}'

	digits, n = shortest_digits(value)
	k = len(digits)

	if k <= n <= 21:
		return f'{digits}{"0" * (n - k)}.0'

	if 0 < n < k:
		return f'{digits[:n]}.{digits[n:]}'

	if -6 < n <= 0:
		return f'0.{"0" * -n}{digits}'

	fraction = f'.{digits[1:]}' if k > 1 else ''
	return f'{digits[0]}{fraction}e{n - 1}'


def neighbours(value: float) -> list[float]:
	return [math.nextafter(value, -math.inf), value, math.nextafter(value, math.inf)]


# The decades where `repr()` and rule 11 switch to an exponent, the ends of the range, and subnormals.
EDGE_FLOATS = sorted(
	{neighbour for exponent in [*range(14, 24), *range(-9, -3)] for neighbour in neighbours(float(f'1e{exponent}'))}
	| {sys.float_info.max, sys.float_info.min, 5e-324, 2.2250738585072009e-308, 9007199254740993.0, 0.1, 1 / 3}
)

layout_floats = st.one_of(
	st.floats(allow_nan=False),
	st.floats(min_value=1e15, max_value=1e23),
	st.floats(min_value=1e-9, max_value=1e-3),
	st.floats(min_value=0, max_value=sys.float_info.min, exclude_min=True),
	st.sampled_from(EDGE_FLOATS),
	st.integers(1, 2**52 - 1).map(lambda mantissa: mantissa * 5e-324),
).flatmap(lambda value: st.sampled_from([value, -value]))


@given(layout_floats)
@scaled(5)
def test_float_layout_matches_an_exact_reference(value: float) -> None:
	text = soml.dumps({'a': value})
	written = text.removeprefix('a: ').removesuffix('\n')
	assert written == reference_float_text(value)
	assert float(written) == value
	assert typed(soml.loads(text)) == typed({'a': value + 0.0})


@pytest.mark.parametrize('value', EDGE_FLOATS)
def test_float_layout_at_the_edges(value: float) -> None:
	assert soml.dumps({'a': value}) == f'a: {reference_float_text(value)}\n'


@pytest.mark.parametrize(
	('value', 'expected'),
	[
		(1e21, '1e21'),
		(1e20, '100000000000000000000.0'),
		(1e-7, '1e-7'),
		(1e-6, '0.000001'),
		(1.5e300, '1.5e300'),
		(5e-324, '5e-324'),
		(123456789012345680000.0, '123456789012345680000.0'),
		(1.7976931348623157e308, '1.7976931348623157e308'),
	],
)
def test_the_reference_matches_the_spec_examples(value: float, expected: str) -> None:
	assert reference_float_text(value) == expected


# Fuzzing


def parse_or_error(document: str | bytes) -> soml.Document | soml.SOMLDecodeError:
	try:
		return soml.loads(document)
	except soml.SOMLDecodeError as error:
		return error


def check_parses_or_fails_cleanly(document: str | bytes) -> None:
	"""Input either parses, and then survives a round trip through `dumps()`, or raises `SOMLDecodeError` with a position inside the document."""
	value = parse_or_error(document)

	if isinstance(value, soml.SOMLDecodeError):
		assert isinstance(document, bytes) or value.doc == document
		assert 0 <= value.pos <= len(value.doc)
		assert value.lineno >= 1
		assert value.colno >= 1
		return

	text = soml.dumps(value)
	assert typed(soml.loads(text)) == typed(value)


fuzz_text = st.lists(st.one_of(st.sampled_from(FUZZ_TOKENS), st.characters()), max_size=40).map(''.join)


@st.composite
def respelled_documents(draw: st.DrawFn) -> str:
	return Respeller(draw(st.randoms(use_true_random=False))).document(draw(documents))


@st.composite
def mutated_documents(draw: st.DrawFn) -> str:
	"""A valid document with characters inserted, deleted, or replaced."""
	characters = list(draw(st.one_of(st.sampled_from(VALID_DOCUMENTS), respelled_documents())))

	for _ in range(draw(st.integers(1, 4))):
		position = draw(st.integers(0, len(characters)))
		token = draw(st.one_of(st.sampled_from(FUZZ_TOKENS), st.characters()))

		match draw(st.sampled_from(['insert', 'delete', 'replace'])):
			case 'insert':
				characters.insert(position, token)
			case 'delete':
				del characters[position : position + 1]
			case _:
				characters[position : position + 1] = [token]

	return ''.join(characters)


@st.composite
def mutated_bytes(draw: st.DrawFn) -> bytes:
	data = bytearray(draw(st.sampled_from(VALID_DOCUMENTS)).encode())

	for _ in range(draw(st.integers(1, 3))):
		position = draw(st.integers(0, len(data)))
		data[position:position] = draw(st.one_of(st.sampled_from(FUZZ_BYTES), st.binary(min_size=1, max_size=2)))

	return bytes(data)


@given(st.one_of(fuzz_text, st.text()))
@scaled(5)
def test_random_text_parses_or_fails_cleanly(document: str) -> None:
	check_parses_or_fails_cleanly(document)


@given(st.one_of(st.binary(max_size=60), mutated_bytes()))
@scaled(3)
def test_random_bytes_parse_or_fail_cleanly(document: bytes) -> None:
	check_parses_or_fails_cleanly(document)


@given(mutated_documents())
def test_mutated_documents_parse_or_fail_cleanly(document: str) -> None:
	check_parses_or_fails_cleanly(document)


@given(st.lists(st.sampled_from(['[', ']', '{', '}', 'a:', 'a.', ' ', ',', '1', '\n']), max_size=300).map(''.join))
def test_deep_bracket_soup_parses_or_fails_cleanly(document: str) -> None:
	check_parses_or_fails_cleanly(f'{"[" * 90}{document}')
	check_parses_or_fails_cleanly(f'a: {document}')


HUGE = 100_000

# Huge and deep documents that must be refused with a position, and without any other exception.
HUGE_INVALID_DOCUMENTS = [
	'[' * HUGE,
	'{' * HUGE,
	'{a: ' * HUGE,
	'[{a: ' * HUGE,
	f'a: {"[" * HUGE}',
	f'{"a." * HUGE}b: 1',
	f'{{{"a." * HUGE}b: 1}}',
	']' * HUGE,
	'1' * HUGE,
	f'a: {"1" * HUGE}',
	f'a: {"9" * 5000}',
	f'a: -{"9" * 5000}',
	f'a: 0x{"F" * HUGE}',
	f'a: 0x{"0" * HUGE}8000000000000000',
	f'a: {"1_" * HUGE}1',
	f'a: {"1_" * HUGE}',
	f'a: 0.{"0" * HUGE}1',
	f'a: 1{"0" * HUGE}.0',
	f'a: 1e{"9" * HUGE}',
	f'a: 1e-{"9" * HUGE}',
	f'a: 1.5e{"0" * HUGE}',
	f'a: {"1" * HUGE}h',
	f'a: -0.{"0" * HUGE}s',
	f'a: 0.{"0" * HUGE}1ns',
	f'a: 0.{"0" * HUGE}1h',
	f'a: 1h{"1m" * HUGE}',
	f'a: 1{"x" * HUGE}',
	f'a: 2026-01-01T00:00:00.{"0" * HUGE}Z',
	f'a: 2026-01-01T00:00:00{"x" * HUGE}',
	f'a: {"2026-01-01" * HUGE}',
	f"a: '{'x' * HUGE}",
	f'a: "{"\\u{" * HUGE}',
	f'# {"x" * HUGE}',
	'/*' * HUGE,
	f'/* {"*" * HUGE}',
	f'a: {"'" * HUGE}',
	f'{"k " * HUGE}',
	'a' * HUGE,
	f'a: {"-" * HUGE}',
	f'a: {"infinity" * HUGE}',
	'\n'.join(f'k{index}: {index}' for index in range(10_000)) + '\nnope',
]

# Huge documents that are valid, with the value they hold, so that a shortcut for long input cannot refuse one by mistake.
HUGE_VALID_DOCUMENTS: list[tuple[str, soml.Document]] = [
	(f'a: 0x{"0" * HUGE}1', {'a': 1}),
	(f'a: 0x{"0" * HUGE}7FFFFFFFFFFFFFFF', {'a': INT64_MAX}),
	(f'a: 0b{"0_" * HUGE}{"1" * 63}', {'a': INT64_MAX}),
	(f'a: 0o{"0" * HUGE}', {'a': 0}),
	(f'a: 1.{"1" * HUGE}', {'a': 1.1111111111111112}),
	(f'a: 1.{"0" * HUGE}e-300', {'a': 1e-300}),
	(f'a: 1.{"0" * HUGE}s', {'a': timedelta(seconds=1)}),
	(f'a: 0.{"0" * HUGE}s', {'a': timedelta(0)}),
	(f'a: 1.5{"0" * HUGE}h', {'a': timedelta(minutes=90)}),
	(f"a: '{'x' * HUGE}'", {'a': 'x' * HUGE}),
	(f'a: "{"\\u{1f600}" * HUGE}"', {'a': '😀' * HUGE}),
	(f'a: "{"\\" * HUGE}"', {'a': '\\' * (HUGE // 2)}),
	(f'a: {'"' * HUGE}\n{'"' * HUGE}', {'a': ''}),
	(f"a: '''\n{'\n' * HUGE}'''", {'a': ''}),
	(f"a: '''\n{' \t\n' * HUGE}\tx\n{'\n' * HUGE}\ty\n\t'''", {'a': f'x{"\n" * (HUGE + 1)}y'}),
	(f"a: ''''\n{"'" * HUGE}\n''''", {'a': "'" * HUGE}),
	(f'{"k" * HUGE}: 1', {'k' * HUGE: 1}),
	(f'a: [{"1, " * HUGE}]', {'a': [1] * HUGE}),
	(f'/* {"x " * HUGE} */\na: 1 # {"x" * HUGE}', {'a': 1}),
]


@pytest.mark.parametrize('document', HUGE_INVALID_DOCUMENTS, ids=[str(index) for index in range(len(HUGE_INVALID_DOCUMENTS))])
def test_huge_and_deep_invalid_input_fails_cleanly(document: str) -> None:
	assert isinstance(parse_or_error(document), soml.SOMLDecodeError)
	check_parses_or_fails_cleanly(document)


@pytest.mark.parametrize(('document', 'expected'), HUGE_VALID_DOCUMENTS, ids=[str(index) for index in range(len(HUGE_VALID_DOCUMENTS))])
def test_huge_valid_input_reads_as_its_value(document: str, expected: soml.Document) -> None:
	assert typed(soml.loads(document)) == typed(expected)


# Each way to nest, as a function of how many levels it nests, and the count that reaches exactly the deepest depth a reader accepts.
NESTING_FORMS: list[tuple[str, Callable[[int], str], int]] = [
	('arrays', lambda count: f'{"[" * count}{"]" * count}', MAX_DEPTH),
	('braced objects', lambda count: f'{"{a: " * count}1{"}" * count}', MAX_DEPTH),
	('arrays in a brace-less object', lambda count: f'a: {"[" * count}{"]" * count}', MAX_DEPTH - 1),
	('braced objects in a brace-less object', lambda count: f'a: {"{b: " * count}1{"}" * count}', MAX_DEPTH - 1),
]


@pytest.mark.parametrize(('create', 'count'), [(create, count) for _, create, count in NESTING_FORMS], ids=[name for name, _, _ in NESTING_FORMS])
def test_the_depth_limit_is_the_same_for_reading_and_writing(create: Callable[[int], str], count: int) -> None:
	value = soml.loads(create(count))
	assert container_depth(value) == MAX_DEPTH
	assert typed(soml.loads(soml.dumps(value))) == typed(value)

	with pytest.raises(soml.SOMLDecodeError, match=f'more than {MAX_DEPTH} levels'):
		soml.loads(create(count + 1))

	deeper = [value]

	with pytest.raises(ValueError, match=f'more than {MAX_DEPTH} levels'):
		soml.dumps(deeper)
