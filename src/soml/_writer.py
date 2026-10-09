"""
The writer, and the conversions to exact builtin types that the writer and the syntax tree share.
"""

import math
import re
from collections import UserString
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from operator import index as exact_int  # Returns an exact int without calling an int subclass's overridden `__int__` or `__index__`.
from operator import itemgetter

from ._parser import INT64_MAX, INT64_MIN, MAX_DEPTH, UNIX_EPOCH, abbreviate, is_bare_key

# Not imported at runtime, so that importing the package stays fast. Annotations are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Iterable, Iterator
	from typing import NoReturn, TypeIs

type Serializable = str | int | float | bool | datetime | date | timedelta | Sequence[Serializable] | Mapping[str, Serializable] | None
"""
A value that `dumps()` accepts. This is wider than what `loads()` returns, so that a `dict[str, int]` or a `tuple` can be written as it is.
"""

# A single quote, a control character, or a lone surrogate, which all need more than `'...'`.
NEEDS_ATTENTION = re.compile("[\x00-\x1f'\x7f\ud800-\udfff]")
LONE_SURROGATE = re.compile('[\ud800-\udfff]')
ESCAPED_CHARACTER = re.compile(r'[\x00-\x1f\x7f\\"]')

ESCAPES = str.maketrans(
	{
		**{chr(code): f'\\u{{{code:x}}}' for code in (*range(32), 127)},
		'\\': '\\\\',
		'"': '\\"',
		'\n': '\\n',
		'\t': '\\t',
	}
)

MICROSECOND = timedelta(microseconds=1)
MAX_DURATION_MICROSECONDS = INT64_MAX // 1000
RADIX_FORMATS = {16: ('0x', 'X'), 8: ('0o', 'o'), 2: ('0b', 'b')}
SCALAR_SEQUENCES = (str, UserString, bytes, bytearray, memoryview)


def dumps(value: Mapping[str, Serializable] | Sequence[Serializable], /, *, canonical: bool = False) -> str:
	"""
	Serialize a `dict` or a `list` to SOML.

	The output follows every rule of canonical form except member order: members keep the order of the input, because a person reads the file, and sorted keys put `description` before `name`. Comments and block strings are never written. The text ends with one line feed.

	With `canonical=True`, members are sorted by key, and the output is canonical form. Canonical form is unique for a value: equal values give the same text, so the output can be hashed, signed, or compared.

	An `int` is written as an int and a `float` as a float, so `{'port': 8080}` gives `port: 8080` and `{'ratio': 1.0}` gives `ratio: 1.0`. A `date` is written as `YYYY-MM-DD`, without a time or timezone. A `datetime` must have a time zone, and it is written in UTC.

	Raises `TypeError` for a value of a type that cannot be written, or when `canonical` is not a `bool`, and `ValueError` for a value of the right type that cannot be written, such as NaN, an int outside the 64-bit range, a string with a carriage return, a `datetime` without a time zone, a circular reference, or nesting more than 100 levels deep.
	"""
	# A runtime check too, for callers that are not type-checked, because a string such as `'false'` is truthy.
	if not isinstance(canonical, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
		raise TypeError(f'The `canonical` option must be a bool, not {type(canonical).__name__!r}')

	return Writer(canonical=canonical).write_document(value)


class Writer:
	"""
	Appends to one list of parts that is joined once at the end, so the time is linear in the output whatever the nesting.
	"""

	__slots__ = ('ancestors', 'canonical', 'parts')

	def __init__(self, *, canonical: bool) -> None:
		self.canonical = canonical
		self.parts: list[str] = []
		# The ids of the containers being written, to find a circular reference.
		self.ancestors: set[int] = set()

	def write_document(self, value: object) -> str:
		check_document_type(value)

		if is_object(value):
			# The root is the first ancestor and is always within the depth limit.
			self.ancestors.add(id(value))
			for key, member in self.members(value):
				self.write_item(f'{format_key(key)}: ', member, 2, '')

			# A brace-less object needs at least one entry, so the empty object keeps its braces.
			return ''.join(self.parts) if self.parts else '{}\n'

		self.write_item('', value, 1, '')
		return ''.join(self.parts)

	def write_item(self, prefix: str, value: object, depth: int, indent: str) -> None:
		"""Writes `prefix`, the value, and a line feed. A container's own depth is `depth`, and its lines are indented by `indent`."""
		scalar = format_scalar(value)

		if scalar is not None:
			self.parts.append(f'{prefix}{scalar}\n')
			return

		enter_container(value, depth, self.ancestors)

		if is_object(value):
			if not len(value):
				self.parts.append(f'{prefix}{{}}\n')
			else:
				inner_indent = f'{indent}\t'
				self.parts.append(f'{prefix}{{\n')
				for key, member in self.members(value):
					self.write_item(f'{inner_indent}{format_key(key)}: ', member, depth + 1, inner_indent)
				self.parts.append(f'{indent}}}\n')
		else:
			array: Sequence[object] = value  # type: ignore[assignment] # `format_scalar()` accepted a container, and it is not an object.
			if not len(array):
				self.parts.append(f'{prefix}[]\n')
			else:
				inner_indent = f'{indent}\t'
				self.parts.append(f'{prefix}[\n')
				for child in array:
					self.write_item(inner_indent, child, depth + 1, inner_indent)
				self.parts.append(f'{indent}]\n')

		# Only the containers being written are ancestors, so a container that two places share is written in both, and only a container inside itself is circular.
		self.ancestors.remove(id(value))

	def members(self, mapping: Mapping[object, object]) -> Iterable[tuple[str, object]]:
		"""The members to write, in the order of the mapping, or sorted by key for canonical form. `str` comparison is by code point, which is the canonical order."""
		members = checked_members(mapping)

		if self.canonical:
			# Note: Not using `sorted(members)` because tuple comparisons were slower for shuffled keys and keys with long common prefixes.
			return sorted(members, key=itemgetter(0))

		return members


def enter_container(value: object, depth: int, ancestors: set[int]) -> None:
	"""Check the shared serialization limits before descending into a container."""
	if depth > MAX_DEPTH:
		raise ValueError(f'Cannot serialize a value nested more than {MAX_DEPTH} levels deep')

	identity = id(value)
	if identity in ancestors:
		raise ValueError('Cannot serialize a circular structure')

	ancestors.add(identity)


def check_document_type(value: object) -> None:
	"""Raises `TypeError` for a document that is not an object or an array, for `dumps()` and `DocumentNode.from_value()`."""
	if not is_object(value) and not is_array(value):
		raise TypeError(f'The top-level value must be a dict or a list, because a document is a collection, not {type(value).__name__!r}')


def is_object(value: object) -> TypeIs[Mapping[object, object]]:
	# The exact type first, which is the common case, because `isinstance` against an abstract class goes through its `__instancecheck__`.
	value_type = type(value)
	if value_type is dict:
		return True

	if value_type is list or value_type is tuple:
		return False

	return isinstance(value, Mapping)


def is_array(value: object) -> TypeIs[Sequence[object]]:
	# Bytes are a sequence of ints in Python, but they are not an array a user would mean to write. Text is not either, and a `UserString` is a sequence of `UserString`s, so it would never end.
	value_type = type(value)
	if value_type is list or value_type is tuple:
		return True

	if value_type is dict:
		return False

	return isinstance(value, Sequence) and not isinstance(value, SCALAR_SEQUENCES)


def checked_members(mapping: Mapping[object, object]) -> Iterable[tuple[str, object]]:
	"""The members of a mapping, in its order, with each key as an exact `str`. Raises `TypeError` for a key that is not a `str`, and `ValueError` for two keys that are the same string, which a `str` subclass with its own `__eq__`, or a mapping that is not a `dict`, can hold."""
	# Exact string keys in a dict are already unique. Only custom mappings and subclass keys need a second set of their normalized keys.
	mapping_type = type(mapping)
	if mapping_type is dict and all(type(key) is str for key in mapping):
		return mapping.items()  # type: ignore[return-value] # All keys were checked to be exact strings.

	return normalized_members(mapping)


def normalized_members(mapping: Mapping[object, object]) -> Iterator[tuple[str, object]]:
	"""Normalize and check keys only when a mapping can hold duplicate string values."""

	keys: set[str] = set()

	for key, value in mapping.items():
		# The exact type first, so that the common key skips the call of `exact_str()`.
		if type(key) is str:
			key_text = key
		else:
			if not isinstance(key, str):
				raise TypeError(f'Object keys must be str, not {type(key).__name__!r}')

			key_text = exact_str(key)

		if key_text in keys:
			raise ValueError(f'Cannot serialize two keys that are the same string: {abbreviate(key_text)!r}')

		keys.add(key_text)
		yield key_text, value


def format_scalar(value: object) -> str | None:
	"""Formats a scalar, or returns `None` for a container. The exact types come first, because they are the common case."""
	value_type = type(value)

	if value_type is str:
		return format_string(value, 'string')  # type: ignore[arg-type] # The exact type was checked.

	if value_type is int:
		return format_integer(value)  # type: ignore[arg-type] # The exact type was checked.

	if value_type is float:
		return format_float(value)  # type: ignore[arg-type] # The exact type was checked.

	if value is None:
		return 'null'

	# `bool` is a subclass of `int`, so it is checked before it.
	if value_type is bool:
		return 'true' if value else 'false'

	if value_type is dict or value_type is list or value_type is tuple:
		return None

	if value_type is datetime:
		return format_instant(value)  # type: ignore[arg-type] # The exact type was checked.

	if value_type is date:
		return date.isoformat(value)  # type: ignore[arg-type] # The exact type was checked.

	if value_type is timedelta:
		return format_duration(value)  # type: ignore[arg-type] # The exact type was checked.

	# Subclasses, such as `StrEnum` and `IntEnum`, are written as their value, not as their `str()`, which an enum may override.
	if isinstance(value, str):
		return format_string(exact_str(value), 'string')

	if isinstance(value, int):
		return format_integer(exact_int(value))

	if isinstance(value, float):
		return format_float(exact_float(value))

	if isinstance(value, datetime):
		return format_instant(value)

	if isinstance(value, date):
		return date.isoformat(value)

	if isinstance(value, timedelta):
		return format_duration(value)

	if is_object(value) or is_array(value):
		return None

	raise_unsupported_type(value)


def raise_unsupported_type(value: object) -> NoReturn:
	"""The same actionable type errors for plain values and syntax-tree construction."""
	raise TypeError(f'Object of type {type(value).__name__!r} is not SOML serializable')


exact_str = str.__str__
"""The value as an exact `str`. The base method, because a subclass, such as an enum that mixes in `str`, may override `__str__` or `__format__`."""


def exact_float(value: int | float) -> float:  # noqa: PYI041 # Written out, because the `int` case has its own branch.
	"""The value as an exact `float`. The base method, because a subclass may override `__float__`. A type checker accepts an `int` for a `float`, so an `int` is converted too."""
	# Note: Not using `float.from_number()` because it can call subclass conversion overrides instead of extracting the base value.
	if type(value) is float:
		return value

	if isinstance(value, float):
		return float.__float__(value)

	try:
		return float(exact_int(value))
	except OverflowError:
		raise ValueError('The integer is too large to be a finite float') from None


def format_key(key: str) -> str:
	return key if is_bare_key(key) else format_string(key, 'key')


def format_string(string: str, kind: str) -> str:
	# One search settles the common case: no quote, no control character, and no lone surrogate.
	if NEEDS_ATTENTION.search(string) is None:
		return f"'{string}'"

	return f'"{escaped_string_body(string, kind)}"'


def format_escaped_string(string: str, kind: str) -> str:
	"""A string in the `"..."` form, even when `'...'` would do."""
	return f'"{escaped_string_body(string, kind)}"'


def escaped_string_body(string: str, kind: str) -> str:
	"""The body of a `"..."` string, with the checks that a `'...'` string skips: no lone surrogate and no carriage return."""
	# Note: Not using `json.dumps()` because JSON's `\uXXXX` escapes are not SOML's `\u{…}` escapes.
	check_string(string, kind)
	if string.isascii():
		return string.translate(ESCAPES)

	# Note: Not using `str.translate()` for Unicode text because it was slower for the sparse escapes typical of config strings.
	return ESCAPED_CHARACTER.sub(lambda match: ESCAPES[ord(match[0])], string)


def check_string(string: str, kind: str) -> None:
	"""Validate a string or key without rendering escaped text that a tree does not need."""
	if not string.isascii() and LONE_SURROGATE.search(string) is not None:
		raise ValueError(f'Cannot serialize a {kind} containing a lone surrogate, which is not a Unicode scalar value')

	if '\r' in string:
		raise ValueError(f'Cannot serialize a {kind} containing a carriage return (U+000D), which is not representable')


def format_integer(value: int) -> str:
	check_integer(value)
	return str(value)


def check_integer(value: int) -> None:
	"""Check the integer range without rendering decimal text that a tree does not need."""
	if not INT64_MIN <= value <= INT64_MAX:
		# Without the value, which can be too long to convert to text.
		raise ValueError('Cannot serialize an integer outside the 64-bit range (-9223372036854775808 to 9223372036854775807)')


def format_float(value: float) -> str:
	"""
	The shortest decimal that reads back as the same float, laid out as ECMAScript `Number::toString` does, which is also RFC 8785's choice, with `.0` added when there is neither a fraction nor an exponent, so that a float never reads back as an int.

	`repr()` gives the digits, but not the layout: it switches to an exponent at other magnitudes, and it writes `e+21` and `e-07`.
	"""
	# Note: Not using `repr()` alone because canonical SOML requires different exponent thresholds and spelling.
	# Covers -0.0 too, since zero has one value whatever its sign.
	if value == 0:
		return '0.0'

	if not math.isfinite(value):
		check_float(value)
		return 'infinity' if value > 0 else '-infinity'

	text = float.__repr__(value)

	if 'e' not in text:
		return text

	sign = '-' if value < 0 else ''
	mantissa, _, exponent_text = text.removeprefix('-').partition('e')
	# The value is 0.{digits} times 10 to the power of n.
	n = int(exponent_text) + 1
	digits = mantissa.replace('.', '')
	k = len(digits)

	if k <= n <= 21:
		return f'{sign}{digits}{"0" * (n - k)}.0'

	if 0 < n <= 21:
		return f'{sign}{digits[:n]}.{digits[n:]}'

	if -6 < n <= 0:
		return f'{sign}0.{"0" * -n}{digits}'

	fraction = f'.{digits[1:]}' if k > 1 else ''
	return f'{sign}{digits[0]}{fraction}e{n - 1}'


def check_float(value: float) -> None:
	"""Reject NaN without computing a canonical spelling."""
	if math.isnan(value):
		raise ValueError('Cannot serialize NaN, which is not representable. Use None for a missing value')


def exact_date(value: date) -> date:
	"""The calendar components as an exact `date`, without invoking a subclass's overrides."""
	return value if type(value) is date else date.fromordinal(date.toordinal(value))


def exact_instant(value: datetime) -> datetime:
	"""The value as an exact `datetime` in UTC, which is what `loads()` returns for an instant."""
	if type(value) is datetime and value.tzinfo is UTC:
		return value

	if datetime.utcoffset(value) is None:
		raise ValueError(f'Cannot serialize the datetime {datetime.isoformat(value)}, because it has no time zone. An instant needs one, such as datetime.UTC')

	# Note: Not using `value.astimezone(UTC)` because a datetime subclass can override it or preserve its subclass type. Base subtraction and addition return an exact datetime.
	try:
		return UNIX_EPOCH + datetime.__sub__(value, UNIX_EPOCH)
	except OverflowError:
		raise ValueError('Cannot serialize the instant, because in UTC it falls outside the years 0001 to 9999') from None


def duration_microseconds(value: timedelta) -> int:
	"""The duration in Python's native precision, checked against SOML's nanosecond range. The base method, because a subclass may override it."""
	# Note: Not using `total_seconds()` because its float result can lose microseconds for large durations.
	microseconds = timedelta.__floordiv__(value, MICROSECOND)

	if not -MAX_DURATION_MICROSECONDS <= microseconds <= MAX_DURATION_MICROSECONDS:
		raise ValueError(f'Cannot serialize the duration {timedelta.__str__(value)}, because it is outside the 64-bit range of nanoseconds, about 292 years either way')

	return microseconds


def exact_duration(value: timedelta) -> timedelta:
	"""The value as an exact `timedelta`, which is what `loads()` returns for a duration."""
	duration_microseconds(value)
	# Unary plus returns a base timedelta without calling a subclass's constructor or overridden operators.
	return value if type(value) is timedelta else timedelta.__pos__(value)


def format_instant(value: datetime) -> str:
	"""An instant in canonical form, using the builtin calendar formatter after exact UTC normalization."""
	text = datetime.isoformat(exact_instant(value), timespec='microseconds')
	return f'{text.removesuffix("+00:00").rstrip("0").removesuffix(".")}Z'


def format_duration(value: timedelta) -> str:
	microseconds = duration_microseconds(value)

	if microseconds == 0:
		return '0s'

	seconds, fraction = divmod(abs(microseconds), 1_000_000)
	hours, rest = divmod(seconds, 3600)
	minutes, seconds = divmod(rest, 60)
	parts = ['-' if microseconds < 0 else '']

	if hours:
		parts.append(f'{hours}h')

	if minutes:
		parts.append(f'{minutes}m')

	if seconds or fraction:
		parts.append(f'{seconds}{format_fraction(fraction)}s')

	return ''.join(parts)


def format_fraction(microseconds: int) -> str:
	"""A duration's fractional second: nothing for zero, otherwise a `.` and six digits without trailing zeros."""
	return '.' + str(microseconds).zfill(6).rstrip('0') if microseconds else ''
