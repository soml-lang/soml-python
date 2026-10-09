"""
The reader: a recursive-descent parser over one `str`, the checks that apply to a whole document, and `SOMLDecodeError`.

It is a close port of the JS reference implementation, soml-js, so the two agree on every document and give the same error message at the same line and column.
"""

import math
import re
from datetime import UTC, date, datetime, timedelta

# Not imported at runtime, so that importing the package stays fast. Annotations are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from typing import NoReturn

type Value = str | int | float | bool | datetime | date | timedelta | list[Value] | dict[str, Value] | None
"""A value in a document."""

type Document = dict[str, Value] | list[Value]
"""A whole document, which is always an object or an array."""

MAX_DEPTH = 100
"""
The deepest nesting the spec allows, exactly: every array and object is one level, including the document's own collection. It also keeps the recursive-descent parser far from a `RecursionError`.
"""

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

NANOSECONDS_PER_SECOND = 1_000_000_000

# The instant range is checked in UTC, so that every instant has a canonical form that can be read back.
UNIX_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
MIN_INSTANT = (date(1, 1, 1).toordinal() - UNIX_EPOCH_ORDINAL) * 86_400 * NANOSECONDS_PER_SECOND
MAX_INSTANT = (date(9999, 12, 31).toordinal() - UNIX_EPOCH_ORDINAL + 1) * 86_400 * NANOSECONDS_PER_SECOND - 1
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

DURATION_UNITS = {
	'h': 3_600_000_000_000,
	'm': 60_000_000_000,
	's': 1_000_000_000,
	'ms': 1_000_000,
	'us': 1000,
	'ns': 1,
}
"""The duration units in their required order, with their length in nanoseconds."""

RADICES = {'x': 16, 'o': 8, 'b': 2}

BARE_KEY_CHARACTERS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-')
LETTERS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
DIGITS = frozenset('0123456789')
NONZERO_DIGIT = re.compile('[1-9]')
BARE_KEY = re.compile(r'[A-Za-z0-9_-]++')
WORDS_WITH_SPACES = re.compile(r'[A-Za-z0-9_\t -]*+')

# What may directly follow a scalar value: whitespace, a separator, a closing bracket, a comment, or the NUL end sentinel.
VALUE_END_CHARACTERS = frozenset(' \t\n,]}#/\0')

# A number, a date, an instant, or a duration is lexed as one maximal run of these characters, and then validated as a whole.
NUMBER_RUN = re.compile(r'[A-Za-z0-9_.:+-]*+')

# The common case, a short decimal int or float such as `8080`, `-3`, or `30.5`. Anything else is left to the general path. At most 18 integer digits, so an int is in range, and 15 fraction digits, so a float cannot underflow here.
PLAIN_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]{0,17}+)(\.[0-9]{1,15}+)?')
# Digit runs never need to give characters back to a delimiter. Possessive repetitions keep their regex stack bounded even for enormous tokens.
RADIX_INTEGER = re.compile(r'0(?:x[0-9A-F]++(?:_[0-9A-F]++)*+|o[0-7]++(?:_[0-7]++)*+|b[01]++(?:_[01]++)*+)')
DECIMAL_DIGIT_RUN = r'[0-9]++(?:_[0-9]++)*+'
NONZERO_DECIMAL = r'[1-9][0-9]*+(?:_[0-9]++)*+'
DECIMAL_MAGNITUDE = f'(?:0|{NONZERO_DECIMAL})'
EXPONENT = f'(?:0|-?{NONZERO_DECIMAL})'
DECIMAL_INTEGER = re.compile(f'-?{DECIMAL_MAGNITUDE}')
FLOAT = re.compile(rf'-?{DECIMAL_MAGNITUDE}(?:\.{DECIMAL_DIGIT_RUN}(?:e{EXPONENT})?|e{EXPONENT})')
# A token that is not a number or an instant was meant as a duration when its first letter could begin a unit, or a day or a week, or it is a year unit. A year unit, as in `1y` or `2years`, is only one when the token or another part follows it, so that a word such as `100yen` stays a string to quote.
DURATION_LIKE = re.compile(r'-?[0-9][0-9_]*+(?:\.[0-9_]*+)?(?:[dhmnsuw]|(?:y|yrs?|years?)(?:$|[0-9]))', re.IGNORECASE)
DURATION_DIGITS = re.compile(DECIMAL_DIGIT_RUN)
DURATION_FRACTION_END = re.compile(r'[1-9](?=[0_]*+$)')
DURATION_UNIT = re.compile(r'[A-Za-z]*+')
LEADING_ZEROS = re.compile(r'(?:0++_?)*+')
SINGLE_QUOTE_RUN = re.compile(r"'++")
DOUBLE_QUOTE_RUN = re.compile(r'"++')

DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# One digit for the hour too, as in `2026-09-19 7:32`, which is more likely a mistyped instant than a date followed by another value.
TIME_PREFIX = re.compile(r'[0-9]{1,2}:[0-9]{2}')
INSTANT = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]++))?(Z|[+-]([0-9]{2}):([0-9]{2}))')
LOCAL_DATE_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]++)?')
# An offset on its own, as after a space in `2026-09-19T14:00:00 Z`.
OFFSET = re.compile(r'Z|[+-][0-9]{2}:[0-9]{2}')
INSTANT_FORMAT = 'An instant is written as 2026-09-19T14:00:00Z, with an optional fraction of up to nine digits and an offset of Z or ±HH:MM'

# The longest token the diagnostic regular expressions look at, so that a huge invalid token is described from its first part.
MAX_DIAGNOSED_LENGTH = 1000

RADIX_NAME = {'x': 'hexadecimal', 'o': 'octal', 'b': 'binary'}
RADIX_ARTICLE = {'x': 'A', 'o': 'An', 'b': 'A'}
NON_HEXADECIMAL_CHARACTER = re.compile('[^0-9a-fA-F_]')
INVALID_RADIX_DIGIT = {'x': NON_HEXADECIMAL_CHARACTER, 'o': re.compile('[^0-7_]'), 'b': re.compile('[^01_]')}

# Uppercase digits too, so that the value of such an escape is checked before its spelling.
UNICODE_ESCAPE = re.compile(r'u\{([0-9A-Fa-f]{1,6})\}')

# Every C0 control character except tab and line feed, and DEL, which are errors anywhere in a document, and a lone surrogate, which a `str` can hold but which is not a Unicode scalar value.
FORBIDDEN_CHARACTER = re.compile('[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]')
CONTROL_CHARACTER = re.compile('[\x00-\x08\x0b-\x1f\x7f]')

ESCAPED_STRING_SPECIAL = re.compile(r'[\n"\\]')
BLANK_LINE = re.compile(r'[\t ]*+')
LOWERCASE_HEXADECIMAL = re.compile('[a-f]')
HEXADECIMAL_DIGITS = re.compile('[0-9a-fA-F]++(?:_[0-9a-fA-F]++)*+')
COLON_AFTER_WHITESPACE = re.compile(r'[\t ]*+:')
COMMENT = re.compile(r'/\*[^*]*+(?:\*(?!/)[^*]*+)*+\*/|#[^\n]*+')
# A comment that was most likely meant as a value, as in `color: #FFF`. A `#` followed by a space, or by another `#`, starts an ordinary comment. A separator or a closing bracket after the value is not part of it.
COMMENT_AS_VALUE = re.compile(r'#[^\t\n #,\]}][^\t\n ,\]}]*+')
# A TOML table header, as in `[server]` or `[[servers]]`, on a line of its own. The name is a valid key, so that the suggestion is valid.
TABLE_HEADER = re.compile(r'[\t ]*+\[\[?([A-Z_a-z][A-Za-z0-9_-]*+)\]\]?[\t ]*+')
# The groups of three digits after a number that a comma was meant to separate, as in `1,000,000`.
THOUSANDS_GROUPS = re.compile(r'(?:,[0-9]{3}(?![0-9]))*+')

KEY_QUOTING_HINT = '. A key that contains characters other than letters, digits, “_”, and “-” must be quoted'
# Characters that have a meaning in the grammar, so a key quoting hint would be wrong for them. The NUL is the parser's end sentinel. A `=` is left out too, because after a key, as in `name=foo`, it was most likely meant as the `:` of INI and TOML, so a key that holds a `=` gets no quoting hint.
QUOTING_EXCLUDED_CHARACTERS = frozenset('"#\'*,/:=[]{}\0')

# The Unicode property Default_Ignorable_Code_Point, which `unicodedata` does not have: characters that render as nothing, such as variation selectors and U+3164 HANGUL FILLER. Some of them are letters or marks, so their category does not show that they are invisible. Generated from `\p{Default_Ignorable_Code_Point}` in Node.js, as the JS reference implementation uses it.
DEFAULT_IGNORABLE = re.compile(
	'[\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]'
)

SIMPLE_ESCAPES = {
	'\\': '\\',
	'"': '"',
	'n': '\n',
	't': '\t',
}

# Characters that a terminal acts on rather than shows: control characters (except tab), bidirectional controls, and lone surrogates. They are replaced in an error message, so that printing the error cannot run a terminal escape.
UNPRINTABLE = re.compile('[\x00-\x08\x0a-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ud800-\udfff]')

# Names for the characters that are invisible or easy to mistake, so an error can say what it found.
CHARACTER_NAMES = {
	0x061C: 'ARABIC LETTER MARK',
	0x85: 'NEXT LINE',
	0xA0: 'NO-BREAK SPACE',
	0xAD: 'SOFT HYPHEN',
	0x1680: 'OGHAM SPACE MARK',
	0x2000: 'EN QUAD',
	0x2001: 'EM QUAD',
	0x2002: 'EN SPACE',
	0x2003: 'EM SPACE',
	0x2004: 'THREE-PER-EM SPACE',
	0x2005: 'FOUR-PER-EM SPACE',
	0x2006: 'SIX-PER-EM SPACE',
	0x2007: 'FIGURE SPACE',
	0x2008: 'PUNCTUATION SPACE',
	0x2009: 'THIN SPACE',
	0x200A: 'HAIR SPACE',
	0x200B: 'ZERO WIDTH SPACE',
	0x200C: 'ZERO WIDTH NON-JOINER',
	0x200D: 'ZERO WIDTH JOINER',
	0x200E: 'LEFT-TO-RIGHT MARK',
	0x200F: 'RIGHT-TO-LEFT MARK',
	0x2028: 'LINE SEPARATOR',
	0x2029: 'PARAGRAPH SEPARATOR',
	0x202A: 'LEFT-TO-RIGHT EMBEDDING',
	0x202B: 'RIGHT-TO-LEFT EMBEDDING',
	0x202C: 'POP DIRECTIONAL FORMATTING',
	0x202D: 'LEFT-TO-RIGHT OVERRIDE',
	0x202E: 'RIGHT-TO-LEFT OVERRIDE',
	0x202F: 'NARROW NO-BREAK SPACE',
	0x205F: 'MEDIUM MATHEMATICAL SPACE',
	0x2060: 'WORD JOINER',
	0x2066: 'LEFT-TO-RIGHT ISOLATE',
	0x2067: 'RIGHT-TO-LEFT ISOLATE',
	0x2068: 'FIRST STRONG ISOLATE',
	0x2069: 'POP DIRECTIONAL ISOLATE',
	0x3000: 'IDEOGRAPHIC SPACE',
	0xFEFF: 'ZERO WIDTH NO-BREAK SPACE',
}


class SOMLDecodeError(ValueError):
	"""
	Raised by `loads()`, `load()`, `format()`, and `soml.tree.parse()` when the input is not a valid document.

	The attributes are the same as those of `json.JSONDecodeError` and `tomllib.TOMLDecodeError`.
	"""

	msg: str
	"""What is wrong, without the position."""

	doc: str
	"""The document. For bytes that are not valid UTF-8, the part before the first invalid byte."""

	pos: int
	"""The index in `doc` where the error is."""

	lineno: int
	"""The 1-based line of the error."""

	colno: int
	"""The 1-based column of the error, counted in code points."""

	def __init__(self, msg: str, doc: str, pos: int) -> None:
		msg = sanitize(msg)
		lineno = doc.count('\n', 0, pos) + 1
		colno = pos - doc.rfind('\n', 0, pos)
		super().__init__(f'{msg} (at line {lineno}, column {colno})')
		self.msg = msg
		self.doc = doc
		self.pos = pos
		self.lineno = lineno
		self.colno = colno
		# A note (PEP 678), so that a traceback shows the line with a caret under the error. `str(error)` stays the message.
		self._code_frame = create_code_frame(doc, pos, lineno)
		self.add_note(self._code_frame)

	@property
	def code_frame(self) -> str:
		"""
		Up to three lines that end at the error, with a caret under its position, as the JS and Rust implementations draw it. A long line is clipped around the position, and characters a terminal would act on are replaced. It is also the error's note, so a traceback shows it. The readme has an example.
		"""
		return self._code_frame

	# Pickling calls the constructor with these arguments, so the error survives being sent to another process.
	def __reduce__(self) -> tuple[type[SOMLDecodeError], tuple[str, str, int]]:
		return (self.__class__, (self.msg, self.doc, self.pos))


CODE_FRAME_CONTEXT_LINES = 2
CODE_FRAME_WIDTH = 100


def create_code_frame(source: str, offset: int, line: int) -> str:
	# Note: Not using `str.splitlines()` because only the error line and up to two preceding lines are needed, and splitting the whole document would process unrelated text.
	line_starts = [source.rfind('\n', 0, offset) + 1]

	while len(line_starts) <= CODE_FRAME_CONTEXT_LINES and line_starts[0] > 0:
		line_starts.insert(0, source.rfind('\n', 0, line_starts[0] - 1) + 1)

	first_line = line - len(line_starts) + 1
	gutter_width = len(str(line))
	output: list[str] = []

	for index, line_start in enumerate(line_starts):
		number = first_line + index
		is_error_line = number == line
		text, pointer = clip_line(source, line_start, find_line_end(source, line_start), offset if is_error_line else line_start)
		# Sanitizing replaces each character with one code point, so clipping first keeps the caret position and avoids processing text outside the window.
		text = sanitize(text)
		gutter = f'{">" if is_error_line else " "} {number:>{gutter_width}} |'
		output.append(f'{gutter} {text}' if text else gutter)

		if is_error_line:
			# The tabs of the line are kept, so the caret lines up whatever the tab width is.
			padding = ''.join(character if character == '\t' else ' ' for character in text[:pointer])
			output.append(f'  {" " * gutter_width} | {padding}^')

	return '\n'.join(output)


def clip_line(source: str, start: int, end: int, pointer: int) -> tuple[str, int]:
	"""A long line, such as a minified document, cut to a window around the pointer, and the pointer in it."""
	if end - start <= CODE_FRAME_WIDTH:
		return source[start:end], pointer - start

	window_start = max(start, min(pointer - CODE_FRAME_WIDTH // 2, end - CODE_FRAME_WIDTH))
	window_end = window_start + CODE_FRAME_WIDTH
	prefix = '…' if window_start > start else ''
	suffix = '…' if window_end < end else ''
	return f'{prefix}{source[window_start:window_end]}{suffix}', pointer - window_start + len(prefix)


def is_bare_key(key: str) -> bool:
	"""Whether a key can be written without quotes: one or more ASCII letters, digits, `_`, or `-`."""
	# Note: Not using `str.strip()` with the allowed characters because it was slower for long keys.
	# Note: Not using `str.isalnum()` after replacing underscores and dashes because normalization copies long keys; the regex checks them without copying.
	return BARE_KEY.fullmatch(key) is not None


def abbreviate(text: str, maximum_length: int = 40) -> str:
	"""Shorten text quoted in an error message, so that a huge token does not make a huge message. The length is in UTF-16 code units, as in JS, so that the messages are the same, and a character that does not fit whole is left out."""
	# Note: Not using UTF-16 encode/decode because it processes the whole token and complicates preserving lone surrogates at the cut.
	# ASCII needs no UTF-16 accounting, and a short string cannot exceed the limit.
	if len(text) <= maximum_length // 2:
		return text

	if text.isascii():
		return f'{text[:maximum_length]}…' if len(text) > maximum_length else text

	length = 0

	for index, character in enumerate(text):
		length += 2 if character > '\uffff' else 1

		if length > maximum_length:
			return f'{text[:index]}…'

	return text


def sanitize(text: str) -> str:
	return UNPRINTABLE.sub('\ufffd', text)


def format_code_point(code_point: int) -> str:
	# Note: Not using `hex().upper().zfill()` because the format specifier expresses uppercase zero padding more directly.
	return f'U+{code_point:04X}'


def describe_character(character: str) -> str:
	"""Describe a character for an error message, such as `“$”` or `U+00A0 (NO-BREAK SPACE)`."""
	if ' ' <= character <= '~':
		return f'“{character}”'

	# The builtin printability check avoids loading unicodedata for an error.
	code_point = ord(character)
	name = CHARACTER_NAMES.get(code_point)

	if name is not None:
		is_whitespace = character.isspace() or character in '\u200b\ufeff'
		return f'{format_code_point(code_point)} ({name}{"; only space, tab, and line feed are whitespace" if is_whitespace else ""})'

	# Control, format, private-use, unassigned, surrogate, separator, and default-ignorable characters are invisible or ambiguous, so they are shown by code point.
	if not character.isprintable() or DEFAULT_IGNORABLE.match(character) is not None:
		return format_code_point(code_point)

	return f'“{character}”'


def describe_key(key: str) -> str:
	import json  # noqa: PLC0415 # Only needed for an error.

	return abbreviate(key) if is_bare_key(key) else json.dumps(abbreviate(key), ensure_ascii=False)


def decode(text: str | bytes | bytearray) -> str:
	if isinstance(text, str):
		# Note: Not using `str(text)` because a subclass, such as an enum that mixes in `str`, may override conversion. The base method returns an exact str without those overrides.
		return str.__str__(text)

	# A runtime check too, for callers that are not type-checked.
	if isinstance(text, (bytes, bytearray)):  # pyright: ignore[reportUnnecessaryIsInstance]
		try:
			# Strict UTF-8, which also rejects encoded surrogates, overlong forms, and code points above U+10FFFF. A BOM is kept, so that it can be rejected rather than silently dropped.
			# Note: Not using `text.decode()` because a subclass may override it and skip strict decoding. With an encoding, `str()` decodes the buffer directly without calling `decode()` or `__str__()` overrides.
			return str(text, 'utf-8')
		except UnicodeDecodeError as error:
			# The decoder's own buffer is exact bytes, so diagnostics do not call methods overridden by a bytes or bytearray subclass.
			valid = error.object[: error.start].decode()

			# A sequence that the end of the input cuts short.
			if error.reason == 'unexpected end of data':
				raise SOMLDecodeError('Incomplete UTF-8 sequence at the end of the input', valid, len(valid)) from None

			raise SOMLDecodeError(f'Invalid UTF-8 byte 0x{error.object[error.start]:02X}', valid, len(valid)) from None

	raise TypeError(f'Expected str or bytes, not {type(text).__name__!r}')


def check_characters(source: str) -> None:
	"""Characters that are errors wherever they appear, so they are checked once, up front."""
	if source.startswith('\ufeff'):
		raise SOMLDecodeError('A byte order mark (BOM) is not allowed', source, 0)

	# ASCII strings cannot contain surrogates, so use the smaller control-character table for the common case.
	match = (CONTROL_CHARACTER if source.isascii() else FORBIDDEN_CHARACTER).search(source)

	if match is None:
		return

	index = match.start()
	code_point = ord(source[index])

	if 0xD800 <= code_point <= 0xDFFF:
		# A control character later in the document is reported first, as in JS.
		control = CONTROL_CHARACTER.search(source, index)

		if control is None:
			raise SOMLDecodeError(f'A lone surrogate ({format_code_point(code_point)}) is not a Unicode scalar value', source, index)

		index = control.start()
		code_point = ord(source[index])

	if code_point == 0x0D:
		raise SOMLDecodeError('A carriage return (U+000D) is not allowed anywhere. Use LF line endings', source, index)

	raise SOMLDecodeError(
		f'A raw control character ({format_code_point(code_point)}) is not allowed anywhere, including in strings and comments. In a string, write it as the escape \\u{{{code_point:x}}} inside "..."',
		source,
		index,
	)


def parse(text: str | bytes | bytearray, *, truncates_nanoseconds: bool = False) -> Document:
	source = decode(text)
	check_characters(source)
	return Parser(source, truncates_nanoseconds=truncates_nanoseconds).parse_document()


def is_quotable_key_character(character: str) -> bool:
	"""Whether a character may be meant as part of a key although a bare key cannot hold it: a visible character that has no meaning in the grammar."""
	if '!' <= character <= '~':
		return character not in QUOTING_EXCLUDED_CHARACTERS

	# The builtin excludes Unicode separators and controls; ASCII space is printable, so it is checked separately.
	return character != ' ' and character.isprintable() and DEFAULT_IGNORABLE.match(character) is None


def without_comments(text: str) -> str:
	"""The text between two tokens with each comment replaced by spaces of the same length, so that its line breaks and commas can be found by position."""
	# The text between tokens is trivia, so a `#` or `/*` there always starts a comment. Most gaps hold none, and the substitution calls back per comment.
	if '#' not in text and '/*' not in text:
		return text

	return COMMENT.sub(lambda match: ' ' * (match.end() - match.start()), text)


def find_line_end(source: str, offset: int) -> int:
	# Note: Not using `str.index()` because exception handling was slower for ordinary final lines without a line feed.
	end = source.find('\n', offset)
	return len(source) if end == -1 else end


def skip_spaces(text: str, index: int) -> int:
	"""The index after the spaces and tabs from `index` on."""
	return BLANK_LINE.match(text, index).end()  # type: ignore[union-attr] # It matches the empty string.


def skip_spaces_back(text: str, index: int) -> int:
	"""The index after the last character before `index` that is not a space or a tab."""
	# Note: Not using `str.rstrip()` on a prefix because copying unrelated source was slower for short indentation after a long line.
	while index > 0 and text[index - 1] in ' \t':
		index -= 1

	return index


def is_value_end(character: str) -> bool:
	"""Whether a character may directly follow a scalar value. An empty string is the end of the document."""
	return not character or character in VALUE_END_CHARACTERS


def is_value_end_at(source: str, index: int) -> bool:
	"""Whether a value ends at `index`, so that a suggestion that ends there leaves nothing out. A `/` only starts a comment with a `*` after it, or a `//` comment of another language, which the error for what follows names, so in `km/h` the text goes on."""
	character = source[index : index + 1]
	return is_value_end(character) and (character != '/' or source[index + 1 : index + 2] in {'*', '/'})


def get_indentation(source: str, offset: int) -> int | None:
	"""The number of spaces and tabs before `offset` on its line, or `None` when something else comes before it."""
	start = skip_spaces_back(source, offset)
	return offset - start if start == 0 or source[start - 1] == '\n' else None


def is_unquoted_text_end(source: str, index: int) -> bool:
	character = source[index]
	is_comment = character == '#' or source.startswith('/*', index)
	return character in {'\n', ',', ']', '}'} or (is_comment and index > 0 and source[index - 1] in {' ', '\t'})


def quoting_example(text: str) -> str:
	"""The example in a message that a string must be quoted, which quotes `text` as a `'...'` string. That cannot hold a `'`, so then there is no simple example."""
	return '' if "'" in text else f", as in '{abbreviate(text)}'"


def is_valid_value(text: str) -> bool:
	"""Whether `text` is one valid value, so that an error message only suggests a fix that works. A value finer than a microsecond is valid, as the spec says, although `loads()` cannot hold it."""
	try:
		value = Parser(f'[{text}]', truncates_nanoseconds=True).parse_document()
	except SOMLDecodeError:
		return False

	return isinstance(value, list) and len(value) == 1


class Parser:
	"""
	A recursive-descent parser over one `str`. Each method reads one grammar rule at `self.index` and moves it past what it read.
	"""

	__slots__ = ('document_start', 'index', 'length', 'padded', 'source', 'truncates_nanoseconds')

	def __init__(self, source: str, *, truncates_nanoseconds: bool = False) -> None:
		self.source = source
		# Whether an instant or a duration finer than a microsecond is truncated to a microsecond instead of rejected, for the formatter, which never reads a value. The values are then wrong, so they must not reach a caller.
		self.truncates_nanoseconds = truncates_nanoseconds
		# The source with a NUL after it, which a valid document cannot hold, so that the parser can read one character past the end without a bounds check.
		self.padded = f'{source}\0'
		self.length = len(source)
		self.index = 0
		# Where the document's collection starts, after the comments and whitespace before it.
		self.document_start = 0

	def fail(self, reason: str, offset: int | None = None) -> NoReturn:
		raise SOMLDecodeError(reason, self.source, self.index if offset is None else offset)

	def character(self, offset: int) -> str:
		"""The character at `offset`, or an empty string outside the source, as JS `charCodeAt()` gives `NaN` there."""
		return self.source[offset] if 0 <= offset < self.length else ''

	def is_at_end(self) -> bool:
		return self.index >= self.length

	def parse_document(self) -> Document:
		self.skip_trivia()
		self.document_start = self.index

		if self.is_at_end():
			self.fail('A document must contain an object or an array, but this one is empty')

		character = self.source[self.index]
		value: Document

		if character == '{':
			value = self.parse_object(1)
		elif character == '[':
			value = self.parse_array(1)
		else:
			value = self.parse_bare_object()

		self.skip_trivia()

		if not self.is_at_end():
			self.fail(f'Unexpected {self.describe_here()} after the end of the document')

		return value

	def parse_bare_object(self) -> dict[str, Value]:
		"""`bare-object = entry ( entry-sep entry )*`, where a separator is one or more line breaks."""
		object_: dict[str, Value] = {}
		start = self.index

		try:
			self.parse_entry(object_, 1)
		except SOMLDecodeError:
			self.diagnose_bare_value(start)
			raise

		while True:
			has_crossed_line_break = self.skip_trivia()

			if self.is_at_end():
				return object_

			if self.source[self.index] == ',':
				self.fail('Top-level entries are separated by line breaks, not commas. Use braces for a one-line object')

			if not has_crossed_line_break:
				self.fail(f'Expected a line break before the next entry, but found {self.describe_here()}{self.slash_comment_hint()}')

			self.parse_entry(object_, 1)

	def diagnose_bare_value(self, start: int) -> None:
		"""A whole document that is one scalar, such as `5` or an instant, fails as an entry. That failure is reported as what it is."""
		self.index = start
		is_bare_value = False

		try:
			self.parse_value(1)
			self.skip_trivia()
			is_bare_value = self.is_at_end()
		except SOMLDecodeError:
			# Not a bare value either, so the original error stands.
			pass

		if is_bare_value:
			self.fail('A bare value is not a document. A document is an object or an array, so write it as `key: value` or `[value]`', start)

	def skip_trivia(self) -> bool:
		"""Skips spaces, tabs, line breaks, and comments, and reports whether a line break was crossed. A line break inside a block comment does not count."""
		source = self.source
		padded = self.padded
		length = self.length
		index = self.index
		has_crossed_line_break = False

		while True:
			character = padded[index]

			if character in ' \t':
				index += 1
			elif character == '\n':
				has_crossed_line_break = True
				index += 1
			elif character == '#':
				end = source.find('\n', index)
				index = length if end == -1 else end
			elif character == '/' and padded[index + 1] == '*':
				index = self.skip_block_comment(index)
			else:
				break

		self.index = index
		return has_crossed_line_break

	def skip_block_comment(self, start: int) -> int:
		source = self.source
		end = source.find('*/', start + 2)

		if end == -1:
			self.fail('Unterminated block comment', start)

		nested = source.find('/*', start + 2, end)

		# The body may not contain `/*`. An opening that overlaps the closing `*/`, as in `/*/`, is not inside the body.
		if nested != -1:
			self.fail('Block comments cannot be nested, and their body may not contain “/*”', nested)

		return end + 2

	def parse_entry(self, object_: dict[str, Value], depth: int) -> None:
		"""`entry = key ":" ws value`."""
		key_start = self.index
		key = self.parse_key()
		character = self.padded[self.index]

		if character == '.':
			self.fail_dot_in_key(key_start)

		if character != ':':
			self.fail_missing_colon(key_start)

		colon = self.index
		self.index += 1
		self.skip_trivia()

		try:
			value = self.parse_value(depth + 1)
		except SOMLDecodeError as error:
			self.diagnose_bad_value(error, key, key_start, colon)
			raise

		if key in object_:
			self.fail(f'Duplicate key {describe_key(key)}', key_start)

		object_[key] = value

	def diagnose_bad_value(self, error: SOMLDecodeError, key: str, key_start: int, colon: int) -> None:
		"""Reports a value that failed to parse as what the entry was meant to be, when that is clear."""
		# A key that contains a `:`, as in `12:30: 'lunch'`, ends at the first `:`, and the rest is read as the value.
		if self.character(colon + 1) in BARE_KEY_CHARACTERS:
			self.diagnose_key_with_colon(key_start)

		self.index = colon + 1
		is_value_on_next_line = self.skip_trivia()
		value_start = self.index
		comment_hint = self.describe_comment_as_value(colon)

		if is_value_on_next_line:
			self.diagnose_missing_value(value_start, key, key_start, comment_hint)

		# A value that was left out at the end of the document or of an object.
		if comment_hint and error.pos == value_start and self.character(value_start) in {'}', ''}:
			self.fail(f'{error.msg}{comment_hint}', value_start)

	def diagnose_key_with_colon(self, key_start: int) -> None:
		"""A bare key followed directly by `:` and more of a key, up to a `:` that ends the key, as in `a:b: 1`."""
		for index in range(key_start, key_start + MAX_DIAGNOSED_LENGTH):
			character = self.character(index)

			if character == ':':
				if self.character(index + 1) in {'\n', ' ', '\t', ''}:
					self.fail(f"A key that contains “:” must be quoted, as in '{abbreviate(self.source[key_start:index])}'", key_start)
			elif character not in BARE_KEY_CHARACTERS:
				return

	def describe_comment_as_value(self, colon: int) -> str:
		"""The hint for a `#` directly after a `:`, as in `color: #FFF`, which starts a comment rather than a value."""
		hash_index = skip_spaces(self.source, colon + 1)

		if self.character(hash_index) != '#':
			return ''

		# Only the start of a long comment is read, which is cut short in the message anyway. Its length is in UTF-16 code units, as in JS, so that a `'` after it is left out at the same place.
		end = hash_index
		length = 0

		while end < self.length and length < MAX_DIAGNOSED_LENGTH and self.source[end] != '\n':
			length += 2 if self.source[end] > '\uffff' else 1
			end += 1

		match = COMMENT_AS_VALUE.match(self.source, hash_index, end)
		return '' if match is None else f'. “#” starts a comment, so a value that starts with “#” must be quoted{quoting_example(match.group())}'

	def diagnose_missing_value(self, start: int, parent: str, key_start: int, comment_hint: str) -> None:
		"""An entry whose value was left out, as in `a:` followed by `'b': 1` on the next line, reads the next key as the value. That failure is reported as what it is."""
		# A YAML block sequence.
		if self.character(start) == '-' and self.character(start + 1) in {' ', '\t'}:
			self.fail('Expected a value, but found a “-” list. An array is written in brackets, as in [80, 443]', start)

		self.index = start

		try:
			key = self.parse_key()
		except SOMLDecodeError:
			# Not a key either, so the original error stands.
			return

		if self.padded[self.index] != ':':
			return

		# Whitespace or the end follows the `:` of a bare key. A digit follows the `:` in an instant such as `2026-09-19T25:00:00Z`, whose own error is more precise. A quoted key cannot be part of a value, so anything may follow its `:`.
		if self.source[start] not in {"'", '"'} and self.padded[self.index + 1] not in {'\n', ' ', '\t', '\0'}:
			return

		hint = comment_hint or self.describe_indented_key(parent, key, key_start, start)
		self.fail(f'Expected a value, but found the key {abbreviate(describe_key(key), 200)}{hint}', start)

	def describe_indented_key(self, parent: str, key: str, key_start: int, start: int) -> str:
		"""The hint for a key at `start` that is indented under the entry whose value is missing, as YAML nests an object."""
		key_indentation = get_indentation(self.source, key_start)
		indentation = get_indentation(self.source, start)

		if key_indentation is None or indentation is None or indentation <= key_indentation:
			return ''

		from ._writer import format_key  # noqa: PLC0415 # Only needed for an error, and the writer imports the parser.

		# The keys are written as in a document, so that the suggestion is valid, rather than as JSON strings like the rest of the message, whose escapes, such as `\b`, are not all valid.
		return f'. Indentation does not nest objects, so write {abbreviate(format_key(parent), 200)}: {{{abbreviate(format_key(key), 200)}: …}}'

	def fail_missing_colon(self, key_start: int) -> NoReturn:
		source = self.source
		next_index = skip_spaces(source, self.index)

		next_character = self.padded[next_index]

		if next_index > self.index and next_character == ':':
			self.fail('Whitespace is not allowed between a key and its “:”')

		# A block comment followed by the ":" was meant to come before it.
		comment_end = source.find('*/', next_index + 2) if source.startswith('/*', next_index) else -1

		if comment_end != -1 and COLON_AFTER_WHITESPACE.match(source, comment_end + 2) is not None:
			self.fail('A comment is not allowed between a key and its “:”', next_index)

		line_end = find_line_end(source, next_index)
		colon = source.find(':', next_index, line_end)

		# A key with a space in it, such as `the name: 1`. The hint is only given for plain words, because quoting a quoted key would change what it means, and for a `:` that whitespace or the end follows, because quoting the words before the `:` in `server localhost:8080` would give a valid document with another meaning. So `the name:1` gets no hint.
		if (
			next_index > self.index
			and colon != -1
			and next_character in BARE_KEY_CHARACTERS
			and self.character(colon + 1) in {' ', '\t', '\n', ''}
			and WORDS_WITH_SPACES.fullmatch(source, key_start, colon) is not None
		):
			key_end = skip_spaces_back(source, colon)
			key = source[key_start : min(key_end, key_start + MAX_DIAGNOSED_LENGTH)]
			self.fail(f"A bare key cannot contain spaces. Quote it, as in '{abbreviate(key)}'", key_start)

		if self.is_at_end() or source[self.index] == '\n':
			self.fail('Expected “:” after the key')

		# A character directly after a bare key is most likely meant to be part of it.
		hint = KEY_QUOTING_HINT if next_index == self.index and source[self.index - 1] not in {"'", '"'} and is_quotable_key_character(self.padded[next_index]) else ''
		self.index = next_index
		self.fail(f'Expected “:” after the key, but found {self.describe_here()}{hint}')

	def parse_key(self) -> str:
		source = self.source
		start = self.index
		character = self.padded[start]

		if character in {"'", '"'}:
			if source.startswith("'''" if character == "'" else '"""', start):
				self.fail('A block string cannot be a key')

			# A key is a string, so it may hold anything a string can, including a line feed written as an escape.
			return self.parse_literal_string() if character == "'" else self.parse_escaped_string()

		match = BARE_KEY.match(source, start)

		if match is None:
			if character == '[':
				self.diagnose_table_header()

			self.fail('Expected a key' if self.is_at_end() else f'Expected a key, but found {self.describe_here()}{self.key_quoting_hint()}{self.slash_comment_hint()}')

		self.index = match.end()
		return match.group()

	def key_quoting_hint(self) -> str:
		"""The hint for a key that starts with a character a bare key cannot hold, such as the `$` in `$schema`, or nothing for a character with another meaning, such as `}`."""
		if not is_quotable_key_character(self.padded[self.index]):
			return ''

		end = self.index
		# The length of the key in UTF-16 code units, because a longer key gets the hint without the example, as in JS.
		length = 0

		while end < self.length and length < MAX_DIAGNOSED_LENGTH and is_quotable_key_character(self.source[end]):
			length += 2 if self.source[end] > '\uffff' else 1
			end += 1

		# The key up to its `:`, so the hint can show it quoted.
		return f"{KEY_QUOTING_HINT}, as in '{abbreviate(self.source[self.index : end])}'" if length < MAX_DIAGNOSED_LENGTH and self.padded[end] == ':' else KEY_QUOTING_HINT

	def fail_dot_in_key(self, key_start: int) -> NoReturn:
		"""A `.` after a key, as in `example.com: 1`. A key is never a path, so the `.` is meant either as part of the key or as nesting."""
		end = self.index

		while end < key_start + MAX_DIAGNOSED_LENGTH and (self.padded[end] in BARE_KEY_CHARACTERS or self.padded[end] == '.'):
			end += 1

		# The suggestions are only given for a bare key whose dots each have a word on both sides, so that both are valid.
		if self.padded[end] != ':' or self.source[key_start] not in BARE_KEY_CHARACTERS:
			self.fail('A key cannot contain “.” unless it is quoted. Quote the whole key, or use braces to nest, as in a: {b: …}')

		key = self.source[key_start:end]
		segments = key.split('.')

		if '' in segments:
			self.fail('A key cannot contain “.” unless it is quoted. Quote the whole key, or use braces to nest, as in a: {b: …}')

		nested = f'{": {".join(segments)}: …{"}" * (len(segments) - 1)}'

		self.fail(f"A bare key cannot contain “.”. Quote it, as in '{abbreviate(key)}', or use braces to nest, as in {abbreviate(nested)}")

	def fail_too_deep(self) -> NoReturn:
		self.fail(f'The document is nested more than {MAX_DEPTH} levels deep')

	def fail_comma_after_line_break(self) -> NoReturn:
		self.fail('A comma must be on the same line as the item before it. The line break already separates the items, so remove the comma')

	def parse_object(self, depth: int) -> dict[str, Value]:
		if depth > MAX_DEPTH:
			self.fail_too_deep()

		start = self.index
		object_: dict[str, Value] = {}
		self.index += 1
		self.skip_trivia()

		while True:
			character = self.padded[self.index]

			if character == '}':
				self.index += 1
				return object_

			if character == '\0':
				self.fail('Unterminated object: expected “}”', start)

			self.parse_entry(object_, depth)
			# A comma, a line break, or both separate items, so an item that the next one follows on its own line needs no comma. A comma must be on the line of the item before it.
			self.parse_separator('}', start)

	def parse_array(self, depth: int) -> list[Value]:
		if depth > MAX_DEPTH:
			self.fail_too_deep()

		start = self.index
		array: list[Value] = []
		self.index += 1
		self.skip_trivia()

		while True:
			character = self.padded[self.index]

			if character == ']':
				self.index += 1
				return array

			if character == '\0':
				self.fail('Unterminated array: expected “]”', start)

			array.append(self.parse_value(depth + 1))
			# A comma, a line break, or both separate items, so an item that the next one follows on its own line needs no comma. A comma must be on the line of the item before it.
			self.parse_separator(']', start)

	def parse_separator(self, closing: str, start: int) -> None:
		"""Read the shared comma or line-break separator of an object or array."""
		item_end = self.index
		has_crossed_line_break = self.skip_trivia()
		character = self.padded[self.index]

		if character == ',':
			if has_crossed_line_break:
				self.fail_comma_after_line_break()

			self.index += 1
			self.skip_trivia()
		elif character == '\0':
			collection = 'object' if closing == '}' else 'array'
			self.fail(f'Unterminated {collection}: expected “{closing}”', start)
		elif character != closing and not has_crossed_line_break:
			item = 'an object member' if closing == '}' else 'an array item'
			self.fail(f'Expected “,”, a line break, or “{closing}” after {item}, but found {self.describe_here()}{self.block_comment_hint(item_end)}')

	def parse_value(self, depth: int) -> Value:
		character = self.padded[self.index]

		if character == '{':
			return self.parse_object(depth)

		if character == '[':
			return self.parse_array(depth)

		start = self.index
		value: Value
		# An int or a float written with digits.
		is_number = False

		if character in {"'", '"'}:
			if self.source.startswith("'''" if character == "'" else '"""', start):
				value = self.parse_block_string(character)
			else:
				value = self.parse_literal_string() if character == "'" else self.parse_escaped_string()
		elif character == 't' and self.is_keyword('true'):
			value = True
		elif character == 'f' and self.is_keyword('false'):
			value = False
		elif character == 'n' and self.is_keyword('null'):
			value = None
		elif character == 'i' and self.is_keyword('infinity'):
			value = math.inf
		elif character == '-' and self.is_keyword('-infinity'):
			value = -math.inf
		elif character in '0123456789-':
			value = self.parse_number_or_instant()
			is_number = isinstance(value, (int, float))
		else:
			self.fail_unexpected_value()

		if self.padded[self.index] not in VALUE_END_CHARACTERS:
			self.fail_after_value(start, is_number=is_number)

		if is_number and self.padded[self.index] in {' ', '\t'}:
			self.diagnose_unit_after_space(start)

		return value

	def fail_after_value(self, start: int, *, is_number: bool) -> NoReturn:
		"""A character that cannot follow the value that starts at `start`."""
		character = self.source[self.index]

		# Go writes microseconds as `µs`, with the micro sign or the Greek letter mu. The duration is only suggested when it is valid, which a number with an exponent or a radix prefix, for example, is not.
		if is_number and character in {'µ', 'μ'} and self.character(self.index + 1) == 's':
			number = self.source[start : min(self.index, start + MAX_DIAGNOSED_LENGTH)]
			self.fail(f'The unit for microseconds is written us{f", as in {abbreviate(number)}us" if self.index - start <= MAX_DIAGNOSED_LENGTH and is_valid_value(f"{number}us") else ""}')

		# A `''` inside a '...' string, as SQL and YAML escape a quote, ends the string.
		if character == "'" and self.source[start] == "'":
			self.fail("There is no '' escape in a '...' string. Write a string that contains ' as \"...\", as in \"it's\"")

		self.fail(f'Unexpected {self.describe_here()} after a value')

	def diagnose_unit_after_space(self, start: int) -> None:
		"""A number followed by a space and a word, such as `512 MiB` or `10 seconds`, which is an error anyway. A word followed by more than spaces, a separator, or a comment is left to the general errors, because it may be a key, as in `a: 1 b: 2`, or a sentence."""
		source = self.source
		unit_start = skip_spaces(source, self.index)
		unit_end = DURATION_UNIT.match(source, unit_start).end()  # type: ignore[union-attr] # It matches the empty string.

		# Only unit names of one or two letters can be durations; the diagnostic needs just a short prefix of anything longer.
		unit = source[unit_start : min(unit_end, unit_start + 41)]

		# A keyword after a number is a missing comma.
		if not unit or not is_value_end_at(source, skip_spaces(source, unit_end)) or unit in {'true', 'false', 'null', 'infinity'}:
			return

		number = source[start : min(self.index, start + 41)]
		# The duration is only suggested when it is valid, which a number with an exponent or a radix prefix, a fraction of a nanosecond, a negative zero, or a value outside the 64-bit range is not. The string is always offered, because a unit such as `m` may mean meters rather than minutes.
		duration = f'{number}{unit}' if self.index - start <= 41 and unit in DURATION_UNITS and is_valid_value(f'{number}{unit}') else '10s'
		self.fail(
			f"A unit cannot follow a number after a space. Write a duration without the space, as in {duration}, and anything else, such as a size, as a string, as in '{abbreviate(f'{number} {unit}')}'",
			unit_start,
		)

	def is_keyword(self, word: str) -> bool:
		if not self.source.startswith(word, self.index):
			return False

		# A longer word, such as `nullable`, is not the keyword.
		if self.padded[self.index + len(word)] in BARE_KEY_CHARACTERS:
			return False

		self.index += len(word)
		return True

	def block_comment_hint(self, item_end: int) -> str:
		"""The hint for an item that the next one follows after a block comment with a line break in it, or after a `//` comment. Without a line break that separates, a line break in the gap is inside a block comment."""
		return '. A line break inside a block comment does not separate items' if self.source.find('\n', item_end, self.index) != -1 else self.slash_comment_hint()

	def fail_unexpected_value(self) -> NoReturn:
		if self.is_at_end():
			self.fail('Expected a value, but reached the end of the document')

		character = self.source[self.index]

		if character == '+':
			self.fail('A “+” sign is not allowed. A number without a sign is positive')

		# A `.` that no digit follows begins a string, such as `.env` or `./foo`, rather than a number.
		if character == '.':
			self.fail('A number cannot begin with “.”; write a digit before it, as in 0.5' if self.character(self.index + 1) in DIGITS else describe_unknown_word('.', self.unquoted_text(self.index)))

		match = BARE_KEY.match(self.source, self.index)

		if match is not None:
			word = self.source[self.index : min(match.end(), self.index + MAX_DIAGNOSED_LENGTH)]

			# A key where a value should be, as when the value of an entry is left out and the next line has a key, or as in `[a: 1]`, is reported as a key. A word after the `:` of a member and a space, as in `msg: Error: file not found` or `url: https://example.com`, is that member's value instead, an unquoted string. Without the space, as in `a:b: 1`, the first `:` was most likely meant as part of the key.
			spaces_start = skip_spaces_back(self.source, self.index)
			is_member_value = spaces_start < self.index and self.character(spaces_start - 1) == ':'

			if not is_member_value and self.padded[match.end()] == ':':
				self.fail(f'Expected a value, but found the key {abbreviate(word)}')

			self.diagnose_table_header(is_value=True)
			self.fail(describe_unknown_word(word, self.unquoted_text(self.index)))

		hint = ". Write a multiline string as a block string, between ''' lines" if self.is_block_scalar_indicator() else self.slash_comment_hint()
		self.fail(f'Expected a value, but found {self.describe_here()}{hint}')

	def unquoted_text(self, start: int) -> str:
		"""The text from `start` that was most likely meant as one unquoted string, such as `John Smith`: up to the end of the line, a comma, a closing bracket, or a comment after a space or a tab, without the spaces and tabs at its end."""
		source = self.source
		end = start
		# The length in UTF-16 code units, as in JS, so that a long text is cut at the same place.
		length = 0

		while end < self.length and length < MAX_DIAGNOSED_LENGTH and not is_unquoted_text_end(source, end):
			length += 2 if source[end] > '\uffff' else 1
			end += 1

		return source[start:end].rstrip(' \t')

	def diagnose_table_header(self, *, is_value: bool = False) -> None:
		"""A TOML table header, as in `[server]` or `[[servers]]` on a line of its own, reads as an array that holds a word, or as a key that starts with "[". Where a value is expected, `is_value`, it is only a table header when its bracket opens the document, because a table cannot be written as an item of an array inside it."""
		source = self.source
		line_start = source.rfind('\n', 0, self.index) + 1

		if is_value and skip_spaces(source, line_start) != self.document_start:
			return

		match = TABLE_HEADER.fullmatch(source, line_start, min(find_line_end(source, self.index), line_start + MAX_DIAGNOSED_LENGTH))

		if match is None:
			return

		name = abbreviate(match.group(1))
		self.fail(f'There are no table headers. Write the table as an object, as in {name}: {{…}}')

	def is_block_scalar_indicator(self) -> bool:
		"""A YAML literal block scalar indicator, as in `key: |` or `key: |-`, at the end of its line."""
		# A folded block scalar, `>`, joins its lines, which a block string does not, so only `|` gets the hint.
		if self.character(self.index) != '|':
			return False

		end = skip_spaces(self.source, self.index + 2 if self.character(self.index + 1) in {'-', '+'} else self.index + 1)
		return self.character(end) in {'\n', ''}

	def slash_comment_hint(self) -> str:
		"""The hint for a `//` comment, as in JavaScript."""
		return '. A comment starts with “#”' if self.source.startswith('//', self.index) else ''

	def describe_here(self) -> str:
		if self.is_at_end():
			return 'the end of the document'

		character = self.source[self.index]
		return 'a line break' if character == '\n' else describe_character(character)

	def parse_literal_string(self) -> str:
		"""`'...'` has no escapes, so its value is the source text between the quotes."""
		# Note: Not using `str.partition()` because it copies the document tail after the closing quote.
		source = self.source
		start = self.index
		# Both searches are bounded by the closing quote, so that a one-line document with many strings stays linear.
		end = source.find("'", start + 1)

		if end == -1 or source.find('\n', start + 1, end) != -1:
			self.fail("Unterminated string. A '...' string must end on the line it starts on; use a block string (''') for multiple lines", start)

		self.index = end + 1
		return source[start + 1 : end]

	def parse_escaped_string(self) -> str:
		source = self.source
		start = self.index
		chunk_start = start + 1
		parts: list[str] | None = None

		while True:
			match = ESCAPED_STRING_SPECIAL.search(source, chunk_start)

			if match is None or match.group() == '\n':
				self.fail('Unterminated string. A "..." string must end on the line it starts on; use a block string (""") for multiple lines', start)

			if match.group() == '"':
				self.index = match.end()

				if parts is None:
					return source[chunk_start : match.start()]

				if chunk_start < match.start():
					parts.append(source[chunk_start : match.start()])
				return ''.join(parts)

			if parts is None:
				parts = []

			if chunk_start < match.start():
				parts.append(source[chunk_start : match.start()])
			text, chunk_start = self.parse_escape(match.start())
			parts.append(text)

	def parse_escape(self, offset: int) -> tuple[str, int]:
		"""Decodes the escape at `offset`, which is a backslash, and returns the text and the index after the escape."""
		# Note: Not using the `unicode_escape` codec because it accepts Python-only escapes and does not support SOML's braced Unicode escapes.
		source = self.source
		character = self.padded[offset + 1]
		simple = SIMPLE_ESCAPES.get(character)

		if simple is not None:
			return simple, offset + 2

		if character == 'u':
			match = UNICODE_ESCAPE.match(source, offset + 1)

			if match is None:
				self.fail(describe_bad_unicode_escape(source[offset + 1 : offset + 13]), offset)

			hex_digits = match.group(1)

			code_point = int(hex_digits, 16)

			# The value is checked before the spelling, so that the uppercase and leading zeros errors never point to an escape that is not allowed either.
			if code_point == 0x0D:
				self.fail(f'A carriage return (U+000D) cannot be represented, so \\u{{{hex_digits}}} is not allowed', offset)

			if 0xD800 <= code_point <= 0xDFFF:
				self.fail(f'\\u{{{hex_digits}}} is a surrogate, which is not a Unicode scalar value', offset)

			if code_point > 0x10FFFF:
				self.fail(f'\\u{{{hex_digits}}} is above U+10FFFF, the largest Unicode scalar value', offset)

			# Note: Not using `str.islower()` because a valid hexadecimal escape may contain only digits.
			if hex_digits != hex_digits.lower():
				self.fail('A Unicode escape uses lowercase hexadecimal digits', offset)

			if len(hex_digits) > 1 and hex_digits.startswith('0'):
				self.fail(f'A Unicode escape may not have leading zeros; write \\u{{{hex_digits.lstrip("0") or "0"}}}', offset)

			return chr(code_point), match.end()

		if character == 'r':
			self.fail('There is no \\r escape, because a carriage return cannot be represented', offset)

		if character == "'":
			self.fail('A \' needs no escape inside "..."', offset)

		if character in {'\0', '\n'}:
			self.fail("A backslash must be followed by an escape character. Use \\\\ for a literal backslash, or a '...' string", offset)

		self.fail(f"Unknown escape “\\{character}”. The escapes are \\\\, \\\", \\n, \\t, and \\u{{…}}; use a '...' string for literal backslashes", offset)

	def parse_block_string(self, quote: str) -> str:
		"""
		A block string opens with a run of three or more quotes and a line break, and closes at the first line whose first non-whitespace content is a run of exactly that many quotes. The closing line's indentation is removed from every content line, except a blank one, which holds only spaces and tabs and becomes an empty line.
		"""
		source = self.source
		start = self.index
		quote_run = SINGLE_QUOTE_RUN if quote == "'" else DOUBLE_QUOTE_RUN
		self.index = quote_run.match(source, start).end()  # type: ignore[union-attr] # A block string starts with quotes.
		delimiter_length = self.index - start

		if self.is_at_end():
			self.fail('Unterminated block string', start)

		# As in Swift, the opening delimiter is followed directly by a line break, not even by trailing whitespace.
		if source[self.index] != '\n':
			self.fail("A block string's opening delimiter must be followed directly by a line break, and its content starts on the next line")

		self.index += 1
		first_line_start = self.index

		# Each content line as a pair of source offsets.
		lines: list[tuple[int, int]] = []
		indentation = ''

		while True:
			# Past the last line, either because the source ended with a line break or because the last line was content.
			if self.index >= self.length:
				self.fail(f'Unterminated block string{self.describe_inline_closing_delimiter(first_line_start, quote, delimiter_length)}', start)

			line_start = self.index
			line_end = find_line_end(source, line_start)
			content_start = BLANK_LINE.match(source, line_start, line_end).end()  # type: ignore[union-attr] # It matches the empty string.
			run = quote_run.match(source, content_start, line_end)
			run_length = 0 if run is None else run.end() - content_start

			if run_length == delimiter_length:
				indentation = source[line_start:content_start]
				self.index = content_start + delimiter_length
				break

			# A blank line is already known from the indentation scan, so it needs no second scan or copied whitespace.
			lines.append((line_end if content_start == line_end else line_start, line_end))
			self.index = line_end + 1

		# Keep source offsets until the final join, instead of copying every line into a second list.
		indentation_length = len(indentation)

		for index, (line_start, line_end) in enumerate(lines):
			# A blank line, which is empty or holds only spaces and tabs, may leave out the indentation, and it becomes an empty line. Swift is stricter here: there only a completely empty line may leave it out.
			if line_start == line_end:
				continue

			if not source.startswith(indentation, line_start, line_end):
				self.fail(
					"This line does not start with the indentation of its block string's closing delimiter. Every line except a blank one must start with exactly the same spaces and tabs", line_start
				)

			lines[index] = (line_start + indentation_length, line_end)

		# Blank lines directly after the opening delimiter and directly before the closing one are not content. Every blank line is empty by now.
		first = 0
		last = len(lines)

		while first < last and lines[first][0] == lines[first][1]:
			first += 1

		while last > first and lines[last - 1][0] == lines[last - 1][1]:
			last -= 1

		if quote == "'":
			return '\n'.join(source[lines[index][0] : lines[index][1]] for index in range(first, last))

		return '\n'.join(self.unescape_line(source[lines[index][0] : lines[index][1]], lines[index][0]) for index in range(first, last))

	def describe_inline_closing_delimiter(self, content_start: int, quote: str, delimiter_length: int) -> str:
		"""The hint for a content line that ends with the delimiter, as TOML allows. Adding a closing line after it would keep the delimiter as content."""
		if delimiter_length > MAX_DIAGNOSED_LENGTH:
			return ''

		source = self.source
		delimiter = quote * delimiter_length
		quote_run = SINGLE_QUOTE_RUN if quote == "'" else DOUBLE_QUOTE_RUN
		line = source.count('\n', 0, content_start) + 1
		line_start = content_start

		while line_start < self.length:
			line_end = find_line_end(source, line_start)
			text_start = skip_spaces(source, line_start)
			text_end = skip_spaces_back(source, line_end)

			# A line of only quotes, longer than the delimiter, is content.
			if source.endswith(delimiter, text_start, text_end) and quote_run.fullmatch(source, text_start, text_end) is None:
				return f'. Its closing delimiter must start a line, so move the {delimiter} at the end of line {line} to a new line'

			line += 1
			line_start = line_end + 1

		return ''

	def unescape_line(self, text: str, offset: int) -> str:
		index = text.find('\\')

		if index == -1:
			return text

		parts: list[str] = []
		chunk_start = 0

		while index != -1:
			if chunk_start < index:
				parts.append(text[chunk_start:index])
			escaped, end = self.parse_escape(offset + index)
			parts.append(escaped)
			chunk_start = end - offset
			index = text.find('\\', chunk_start)

		if chunk_start < len(text):
			parts.append(text[chunk_start:])
		return ''.join(parts)

	def parse_number_or_instant(self) -> int | float | datetime | date | timedelta:
		source = self.source
		start = self.index
		plain = PLAIN_NUMBER.match(source, start)

		# The common case, which the general path below would also read, but more slowly.
		if plain is not None:
			end = plain.end()

			# `-0` is an error, which the general path reports.
			if self.padded[end] in VALUE_END_CHARACTERS:
				text = plain.group()

				if text != '-0':
					self.index = end

					if plain.start(1) == -1:
						return int(text)

					# Negative zero is the same value as zero.
					return float(text) or 0.0

		end = NUMBER_RUN.match(source, start + 1).end()  # type: ignore[union-attr] # It matches the empty string.
		text = source[start:end]
		self.index = end

		if DATE.match(text) is not None:
			# A whole date token followed by no time, even after spaces or tabs, is a date value. A date followed by a time still gets the separator diagnostic.
			if len(text) == 10 and TIME_PREFIX.match(source, skip_spaces(source, end)) is None:
				try:
					return date.fromisoformat(text)
				except ValueError:
					self.fail(describe_date(text), start)

			return self.parse_instant(text, start)

		# Every duration unit ends in `h`, `m`, or `s`, and no int or float does, because hexadecimal digits are uppercase, so a duration skips their checks.
		if text[-1] not in 'hms':
			if RADIX_INTEGER.fullmatch(text) is not None:
				return self.parse_integer(text, start)

			if DECIMAL_INTEGER.fullmatch(text) is not None:
				if text == '-0':
					self.fail('“-0” is not allowed, because zero has one spelling: 0', start)

				return self.parse_integer(text, start)

			if FLOAT.fullmatch(text) is not None:
				return self.parse_float(text, start)

		if DURATION_LIKE.match(text) is not None:
			return self.parse_duration(text, start)

		self.fail(self.describe_thousands_separator(text, start) or describe_bad_number(text, self.unquoted_text(start)), start)

	def describe_thousands_separator(self, text: str, start: int) -> str | None:
		"""A comma used as a thousands separator, as in `[1,000]`, makes the group after it a separate item, which fails only when it has a leading zero. Writing that item in octal, as the leading zero message suggests, would parse to the wrong items."""
		if re.fullmatch('0[0-9]{2}', text) is None or self.character(start - 1) != ',':
			return None

		def get_group_start(group_end: int) -> int:
			group_start = group_end

			# Four digits are already too many for a group, so no earlier digit can change this diagnostic.
			while group_end - group_start < 4 and self.character(group_start - 1) in DIGITS:
				group_start -= 1

			return group_start

		group_end = start - 1
		number_start = get_group_start(group_end)

		# Earlier groups of three digits, as in `12,345,000`.
		while group_end - number_start == 3 and self.character(number_start - 1) == ',' and self.character(number_start - 2) in DIGITS:
			group_end = number_start - 1
			number_start = get_group_start(group_end)

		before = self.character(number_start - 1)

		# The first group has one to three digits, which are not the end of a float, of a number with underscores, or of a number with a radix prefix.
		if number_start == group_end or before in {'.', '_'} or number_start < group_end - 3 or before in LETTERS:
			return None

		# The sign belongs to the number, as in `-1,000`.
		if before == '-':
			number_start -= 1

		groups_start = start + len(text)
		groups = THOUSANDS_GROUPS.match(self.source, groups_start, groups_start + MAX_DIAGNOSED_LENGTH).group()  # type: ignore[union-attr] # It matches the empty string.
		number_end = groups_start + len(groups)
		# At most 19 digits, six commas, and a sign can fit int64. A longer number needs no copied token or speculative parse to decide that neither suggestion is valid.
		number = self.source[number_start : min(number_end, number_start + MAX_DIAGNOSED_LENGTH)]
		reason = f'Leading zeros are not allowed in a decimal number. A comma separates items, so {abbreviate(number)} is not one number'

		if number_end - number_start > 26:
			return reason
		# Both spellings are the same int, so they are suggested only when it is in range.
		with_underscores = number.replace(',', '_')
		return f'{reason}. Write it as {abbreviate(with_underscores)} or {abbreviate(number.replace(",", ""))}' if is_valid_value(with_underscores) else reason

	def parse_integer(self, text: str, start: int) -> int:
		"""
		An int that is already valid. More than 64 significant digits is out of range in any radix, which is decided before `int()`, which refuses more than 4300 decimal digits.
		"""
		is_radix = len(text) > 1 and text[1] in RADICES
		value: int | None = None

		if is_radix:
			first = LEADING_ZEROS.match(text, 2).end()  # type: ignore[union-attr] # It matches the empty string.
			# At most 64 digits with 63 underscores can fit. Strip the leading zeros before copying the bounded significant suffix; the range check below handles its value.
			if len(text) - first <= 127:
				value = int(text[first:] or '0', RADICES[text[1]])
		# A sign and 20 digits with separators is already past the 64-bit range, so a longer number is out of range without converting it. Short tokens are safe to convert directly.
		elif len(text) <= 41:
			value = int(text)

		if value is None or not INT64_MIN <= value <= INT64_MAX:
			self.fail(f'The integer {abbreviate(text)} is outside the 64-bit range (-9223372036854775808 to 9223372036854775807)', start)

		return value

	def parse_float(self, text: str, start: int) -> float:
		"""A float that is already valid. `float()` rounds correctly for input of any length."""
		value = float(text)

		if math.isinf(value):
			self.fail(f'{abbreviate(text)} is too large to be a finite float. Use {"-" if value < 0 else ""}infinity if you mean it', start)

		if value == 0:
			# A nonzero digit before the exponent means the literal is not zero, so it underflowed.
			exponent = text.find('e')
			if NONZERO_DIGIT.search(text, 0, len(text) if exponent == -1 else exponent) is not None:
				self.fail(f'{abbreviate(text)} is too small to be told apart from zero. Write 0.0 if you mean zero', start)

			# Negative zero is the same value as zero.
			return 0.0

		return value

	def find_offset_after_space(self) -> str | None:
		"""An offset on its own after spaces, as in `2026-09-19T14:00:00 Z`, which belongs directly after the time, or `None`."""
		source = self.source
		offset_start = skip_spaces(source, self.index)

		if offset_start == self.index:
			return None

		match = OFFSET.match(source, offset_start)
		return match.group() if match is not None and is_value_end_at(source, match.end()) else None

	def parse_instant(self, text: str, start: int) -> datetime:
		match = None if len(text) > MAX_DIAGNOSED_LENGTH else INSTANT.fullmatch(text)

		if match is None:
			# A date, spaces or tabs, and a time, as TOML and Python write a date and time.
			time_start = skip_spaces(self.source, self.index)
			end = NUMBER_RUN.match(self.source, time_start).end() if time_start > self.index and self.character(time_start) in DIGITS else self.index  # type: ignore[union-attr] # It matches the empty string.
			time = self.source[time_start : min(end, time_start + MAX_DIAGNOSED_LENGTH + 1)]
			# More of the instant may follow, as in `14:00:00,5Z` or `14:00:00[Europe/Oslo]`, so it may have an offset.
			is_whole_value = is_value_end_at(self.source, end) and not (self.character(end) == ',' and self.character(end + 1) in DIGITS)
			self.fail(describe_bad_instant(text, time, is_whole_value=is_whole_value, offset=self.find_offset_after_space()), start)

		hour, fraction, offset, offset_minute = match.group(4, 7, 8, 10)

		# The grammar is already checked. Use the native ISO parser for valid instants, while keeping the precise field diagnostics below for invalid ones. Python also accepts hour 24, normalizes offset minutes, and accepts an unknown offset, so check those SOML constraints first.
		if hour <= '23' and (fraction is None or len(fraction) <= 9) and (offset == 'Z' or offset_minute <= '59') and offset != '-00:00':
			try:
				value = datetime.fromisoformat(text).astimezone(UTC)
			except ValueError, OverflowError:
				pass
			else:
				if fraction and fraction[6:].strip('0') and not self.truncates_nanoseconds:
					self.fail(f"Invalid instant {abbreviate(text)}: it is finer than a microsecond, which Python's datetime cannot hold", start)
				return value

		year, month, day, hour, minute, second, fraction, offset, offset_hour, offset_minute = match.groups()

		def check(is_valid: bool, reason: str) -> None:  # noqa: FBT001
			if not is_valid:
				self.fail(f'Invalid instant {abbreviate(text)}: {reason}', start)

		check(fraction is None or len(fraction) <= 9, 'a fractional second has at most nine digits')
		date_reason = invalid_date_reason(int(year), int(month), int(day))
		if date_reason is not None:
			self.fail(f'Invalid instant {abbreviate(text)}: {date_reason}', start)
		# Each field has exactly two digits, so comparing the text compares the values.
		check(hour <= '23', 'the hour must be 00 to 23')
		check(minute <= '59', 'the minute must be 00 to 59')
		check(second <= '59', 'the second must be 00 to 59, and a leap second is not representable')

		if offset != 'Z':
			check(offset_hour <= '23', 'the offset hour must be 00 to 23')
			check(offset_minute <= '59', 'the offset minute must be 00 to 59')
			check(offset != '-00:00', '-00:00 means “offset unknown” in RFC 3339, which is not representable; use Z or +00:00')

		# `datetime` raises `OverflowError` when the offset moves the instant out of its year range. Every field is valid here, so that is the remaining failure.
		self.fail(f'Invalid instant {abbreviate(text)}: in UTC it falls outside the years 0001 to 9999', start)

	def parse_duration(self, text: str, start: int) -> timedelta:
		"""A scan of the parts in order, so that each error names the part it is about."""

		def fail(reason: str) -> NoReturn:
			self.fail(f'Invalid duration {abbreviate(text)}: {reason}', start)

		is_negative = text.startswith('-')
		index = 1 if is_negative else 0
		previous_size = DURATION_UNITS['h'] + 1
		total = 0
		# The fraction of the last part, and that part's unit size. Only the last part may have one, so the value checks wait until every part is read.
		fraction = ''
		size = 0

		while index < len(text):
			# The integer part is `0` or digits without a leading zero. Anything after a leading `0` is rejected below.
			if text[index] == '0':
				digits_end = index + 1
			else:
				digits_match = DURATION_DIGITS.match(text, index)
				digits_end = index if digits_match is None else digits_match.end()
			next_character = text[digits_end : digits_end + 1]

			if digits_end == index:
				character = text[index]

				if character == '-' and text[index + 1 : index + 2].isdigit():
					fail('only the whole duration takes a sign, as in -1h30m')

				fail('a “+” sign is not allowed' if character == '+' else f'expected a number at “{abbreviate(text[index:], 10)}”')

			if text[index] == '0' and (next_character.isdigit() or next_character == '_'):
				fail('leading zeros are not allowed')

			if next_character == '_':
				fail('an underscore must be between two digits')

			fraction = ''
			unit_start = digits_end

			if next_character == '.':
				fraction_match = DURATION_DIGITS.match(text, digits_end + 1)

				if fraction_match is None:
					fail('a “.” must be followed by a digit')

				unit_start = fraction_match.end()

				if text.startswith('_', unit_start):
					fail('an underscore must be between two digits')

				# Trailing zeros change nothing, and leaving them out keeps the arithmetic small however many there are. Search within the source before copying only the significant prefix.
				if unit_start - digits_end <= 28:
					fraction = fraction_match.group().rstrip('0_').replace('_', '')
				else:
					fraction_end = DURATION_FRACTION_END.search(text, digits_end + 1, unit_start)
					fraction = '' if fraction_end is None else text[digits_end + 1 : min(fraction_end.end(), digits_end + 28)].replace('_', '')

			unit_end = DURATION_UNIT.match(text, unit_start).end()  # type: ignore[union-attr] # It matches the empty string.

			# Valid units need at most two letters; an unknown unit is abbreviated to ten in the diagnostic.
			unit = text[unit_start:unit_end] if unit_end - unit_start <= 11 else text[unit_start : unit_start + 11]

			if unit not in DURATION_UNITS:
				fail(describe_bad_duration_unit(unit, text))

			size = DURATION_UNITS[unit]

			if size >= previous_size:
				fail('the units are in the order h, m, s, ms, us, ns, and each appears at most once')

			if next_character == '.' and text[unit_end : unit_end + 1].isdigit():
				fail('only the last part may have a fraction')

			# More than 19 digits is out of range in any unit, which is decided before `int()`, which refuses more than 4300 digits.
			digit_count = digits_end - index - text.count('_', index, digits_end)
			total += INT64_MAX * 2 if digit_count > 19 else int(text[index:digits_end]) * size
			index = unit_end
			previous_size = size

		# More than 13 fraction digits without a trailing zero is finer than a nanosecond in any unit, which is decided before `int()` sees a huge digit string.
		if len(fraction) > 13:
			fail('it is not a whole number of nanoseconds')

		if fraction:
			whole, rest = divmod(int(fraction) * size, 10 ** len(fraction))

			if rest != 0:
				fail('it is not a whole number of nanoseconds')

			total += whole

		if is_negative and total == 0:
			fail('“-” is not allowed before zero, because zero has one spelling: 0s')

		if total > (-INT64_MIN if is_negative else INT64_MAX):
			fail('it is outside the 64-bit range of nanoseconds, about 292 years either way')

		microseconds, rest = divmod(total, 1000)

		if rest != 0 and not self.truncates_nanoseconds:
			fail("it is finer than a microsecond, which Python's timedelta cannot hold")

		return timedelta(microseconds=-microseconds if is_negative else microseconds)


def describe_bad_duration_unit(unit: str, text: str) -> str:
	"""`text` is the whole duration token."""
	if not unit:
		return 'every number needs a unit: h, m, s, ms, us, or ns'

	if unit in {'d', 'day', 'days'}:
		return 'there is no day unit, because a day is not a fixed length. Write 24h for a fixed 24 hours'

	if unit in {'w', 'week', 'weeks'}:
		return 'there is no week unit, because a day is not a fixed length. Write 168h for a fixed 168 hours'

	lowercase = unit.lower()
	if lowercase in {'y', 'yr', 'yrs', 'year', 'years'}:
		return 'there is no year unit, because a year is not a fixed length. Write 8760h for a fixed 365 days'

	if unit == 'M':
		# A number with `M` alone is more often a size, as in `memory: 512M`, and `512m` would read as minutes.
		size_hint = f". A size, such as 512M, is a string: '{abbreviate(text)}'" if len(text) <= MAX_DIAGNOSED_LENGTH and re.fullmatch('[0-9][0-9_]*M', text) is not None else ''
		return f'there is no month unit, because a month is not a fixed length{size_hint}'

	if lowercase in DURATION_UNITS:
		return f'the units are lowercase: {lowercase}'

	return f'“{abbreviate(unit, 10)}” is not a unit. The units are h, m, s, ms, us, and ns, and a string must be quoted'


def describe_unknown_word(word: str, text: str) -> str:
	"""`text` is the unquoted string that `word` begins, which the suggestion quotes."""
	# A keyword hint is only for the word on its own. In `Yes please` or `Nan Goldin`, following it would leave text behind or change the value.
	lowercase = word.lower() if text == word else ''

	if lowercase in {'true', 'false', 'yes', 'no', 'on', 'off'}:
		return f'“{word}” is not a value. Booleans are written true and false, in lowercase'

	if lowercase in {'null', 'nil', 'none', 'undefined'}:
		return f'“{word}” is not a value. Null is written null, in lowercase'

	if lowercase == 'nan':
		return 'NaN is not representable. Use null for a missing value'

	if lowercase in {'inf', 'infinity'}:
		return f'“{word}” is not a value. Infinity is written infinity, in lowercase'

	return f'Unexpected “{abbreviate(word)}”. A string value must be quoted{quoting_example(text)}'


def describe_bad_unicode_escape(rest: str) -> str:
	"""`rest` is the text after the backslash."""
	if re.match('u[0-9A-Fa-f]{4}', rest) is not None:
		return describe_four_digit_escape(rest)

	if re.match(r'u\{[0-9A-Fa-f]*[A-F]', rest) is not None:
		return 'A Unicode escape uses lowercase hexadecimal digits'

	if rest.startswith('u{}'):
		return 'A Unicode escape needs one to six hexadecimal digits'

	if re.match(r'u\{[0-9a-f]{7}', rest) is not None:
		return 'A Unicode escape has at most six hexadecimal digits'

	return 'A Unicode escape is written \\u{…} with one to six lowercase hexadecimal digits'


def describe_four_digit_escape(rest: str) -> str:
	"""The JSON form `\\uXXXX`, from its `u`, with the escape to write instead. JSON writes a character above U+FFFF as two of them, a surrogate pair, which is one `\\u{…}` escape here. Here, a lone surrogate and a carriage return have no escape."""
	code = int(rest[1:5], 16)
	low = rest[7:11] if re.match(r'\\u[0-9A-Fa-f]{4}', rest[5:]) is not None else None
	low_code = int(low, 16) if low is not None else 0

	if 0xD800 <= code <= 0xDBFF and 0xDC00 <= low_code <= 0xDFFF:
		code_point = 0x10000 + ((code - 0xD800) * 0x400) + (low_code - 0xDC00)
		return f'The four-digit \\{rest[:5]}\\u{low} form is not an escape. Write \\u{{{code_point:x}}}'

	form = f'The four-digit \\{rest[:5]} form is not an escape'

	if 0xD800 <= code <= 0xDFFF:
		return f'{form}, and a lone surrogate is not a Unicode scalar value. Write the character it is half of as one \\u{{…}} escape'

	return f'{form}, and a carriage return (U+000D) cannot be represented' if code == 0x0D else f'{form}. Write \\u{{{code:x}}}'


def describe_bad_number(full_text: str, unquoted_text: str) -> str:
	"""`unquoted_text` is the unquoted string that `full_text` begins, for the suggestion to quote it."""
	text = full_text[:MAX_DIAGNOSED_LENGTH]

	if '+' in text:
		return 'A “+” sign is not allowed in a number, including in an exponent'

	if text.removeprefix('-').startswith(('0B', '0O', '0X')):
		return 'A number prefix is lowercase: 0x, 0o, or 0b'

	# The whole number, not the part cut at the diagnosed length, because a cut can end before the digit or the underscore that the message is about. Each check below takes linear time.
	radix_match = re.match('(-?)0([box])', full_text)

	if radix_match is not None:
		sign, radix = radix_match.groups()
		digits_start = radix_match.end()
		name = RADIX_NAME[radix]

		if sign == '-':
			return f'{RADIX_ARTICLE[radix]} {name} integer cannot have a sign, because it states a bit pattern rather than a quantity'

		if digits_start == len(full_text):
			return f'Expected {name} digits after “0{radix}”'

		invalid = INVALID_RADIX_DIGIT[radix].search(full_text, digits_start)

		if radix == 'x' and invalid is None and LOWERCASE_HEXADECIMAL.search(full_text, digits_start) is not None:
			# The uppercase spelling is only suggested when it is valid, so a misplaced underscore is reported first, and a value outside the 64-bit range gets no example.
			if HEXADECIMAL_DIGITS.fullmatch(full_text, digits_start) is None:
				return 'An underscore in a number must be between two digits'

			first = LEADING_ZEROS.match(full_text, digits_start).end()  # type: ignore[union-attr] # It matches the empty string.
			is_in_range = len(full_text) - first <= 31 and int(full_text[first:] or '0', 16) <= INT64_MAX
			uppercase = f'0x{abbreviate(full_text)[2:].upper()}'
			return f'Hexadecimal digits are uppercase: {uppercase}' if is_in_range else 'Hexadecimal digits are uppercase'

		# A lowercase hexadecimal digit is a digit in the wrong case, so the one named is a character that is no digit in either case.
		return 'An underscore in a number must be between two digits' if invalid is None else f'Invalid {name} digit “{invalid.group()}”'

	# Only when the “E” is where an exponent marker goes, with digits, a `-`, or nothing after it. In `5EUR` or `10EB`, it is part of a word, so lowercasing it would not help.
	if re.fullmatch(r'-?[0-9][0-9_]*(?:\.[0-9_]+)?E-?[0-9_]*', text) is not None:
		return 'An exponent marker is a lowercase “e”'

	if re.match(r'-?[0-9_]+\.(?:$|[^0-9_])', text) is not None:
		return 'A decimal point must be followed by a digit'

	if text.startswith('-.'):
		return 'A number cannot begin with “.”; write a digit before it, as in -0.5'

	if re.fullmatch('[0-9]+(?::[0-9]+)+', text) is not None:
		return (
			'A time of day is a string, so it must be quoted'
			if re.fullmatch(r'[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?', text) is not None
			else f"Invalid number “{abbreviate(text)}”. A value that contains “:” must be quoted, as in '{abbreviate(text)}'"
		)

	# A value with a `:` that starts with a zero, such as the MAC address `00:1A:2B`, is not a number with a leading zero, because removing the zero would not make it valid.
	if re.match('-?0[0-9_]', text) is not None and ':' not in text:
		# Removing the zero gives a valid number with a different meaning, so the message says what the zero usually meant.
		# A `'...'` string cannot hold a `'`, so then there is no example.
		identifier = f'an identifier, such as a ZIP code, as a string{"" if "'" in unquoted_text else f": '{abbreviate(unquoted_text)}'"}'
		# The octal suggestion is only for the number on its own, not for one that more text follows, as in `0412 345 678`, and only when it is in range. It is decided on the whole number, because a cut can end before a digit that is not octal or that changes the value.
		if unquoted_text == full_text and re.fullmatch('0[0-7]+', full_text) is not None:
			first = LEADING_ZEROS.match(full_text).end()  # type: ignore[union-attr] # It matches the empty string.

			if len(full_text) - first <= 21:
				digits = full_text[first:] or '0'

				if int(digits, 8) <= INT64_MAX:
					return f'Leading zeros are not allowed in a decimal number. Write an octal number, such as a file mode, as 0o{digits}, and {identifier}'

		return f'Leading zeros are not allowed in a decimal number. Write {identifier}' if re.fullmatch('0[0-9]+', text) is not None else 'Leading zeros are not allowed in a decimal number'

	# Only in a number: an underscore next to a letter other than the exponent marker is part of a word, such as `4k_video`.
	if re.search('_(?:$|[^0-9])|(?:^|[^0-9])_', text) is not None and re.search(r'[^0-9\-._e]', text) is None:
		return 'An underscore in a number must be between two digits'

	if re.match(r'-?[0-9][0-9_]*(?:\.[0-9_]+)?e-?0_?[0-9]', text) is not None:
		return 'Leading zeros are not allowed in an exponent'

	if re.fullmatch(r'-?[0-9][0-9_]*(?:\.[0-9_]+)?e-0', text) is not None:
		return '“e-0” is not allowed, because an exponent of zero has one spelling: e0'

	# Only a number ends with its exponent marker. A word that ends with an “e”, such as `1byte` or `-verbose`, has no exponent.
	if re.fullmatch(r'-?[0-9][0-9_]*(?:\.[0-9_]+)?e-?', text) is not None:
		return 'Expected digits after the exponent marker “e”'

	if text.count('.') > 1:
		return f'Invalid number “{abbreviate(text)}”. A value with several dots, such as a version number, must be quoted'

	if text.startswith('-') and (len(text) == 1 or text[1] not in DIGITS):
		# The whole word, as for one without the “-”, so that a word such as `-nano` or `-info` is not taken for NaN or infinity.
		lowercase = text.lower()
		if lowercase == '-nan':
			return 'NaN is not representable. Use null for a missing value'

		if lowercase in {'-inf', '-infinity'}:
			return f'“{abbreviate(text)}” is not a value. Negative infinity is written -infinity'

		return 'Expected a digit or “infinity” after “-”'

	if re.search('[A-Za-z]', text) is not None:
		return f'Invalid number “{abbreviate(text)}”. A string value must be quoted{quoting_example(unquoted_text)}'

	return f'Invalid number “{abbreviate(text)}”'


def invalid_date_reason(year: int, month: int, day: int) -> str | None:
	"""The reason the first invalid proleptic Gregorian component is invalid, or `None` when all three are in range. Shared by dates and instants, and ranges are checked before calendar arithmetic."""
	if year < 1 or year > 9999:
		return 'the year must be 0001 to 9999'

	if not 1 <= month <= 12:
		return 'the month must be 01 to 12'

	from calendar import monthrange  # noqa: PLC0415 # Only needed for invalid-date diagnostics.

	last_day = monthrange(year, month)[1]
	return None if 1 <= day <= last_day else f'the day must be 01 to {last_day} in that month'


def describe_date(text: str) -> str:
	"""An exact date token whose year, month, or day is outside the calendar's range."""
	year, month, day = map(int, text.split('-'))
	reason = invalid_date_reason(year, month, day)
	assert reason is not None  # noqa: S101 # The caller only calls this for an invalid date.
	return f'Invalid date {text}: {reason}'


def describe_bad_instant(text: str, time: str, *, is_whole_value: bool, offset: str | None) -> str:
	"""`time` is the token after a space that follows `text`, or an empty string. `is_whole_value` is whether nothing that may be part of the instant follows `text`. `offset` is an offset after a space that follows `text`, or `None`."""
	if len(text) > MAX_DIAGNOSED_LENGTH:
		return f'Invalid instant “{abbreviate(text)}”. {INSTANT_FORMAT}'

	if DATE.fullmatch(text) is not None:
		return describe_space_separated_instant(text, time, is_whole_value=is_whole_value)

	if text.startswith('t', 10):
		return 'The date and time separator in an instant is an uppercase “T”'

	if text.endswith('z'):
		return 'The UTC offset in an instant is an uppercase “Z”'

	if re.search('[+-][0-9]{4}$', text) is not None:
		return "An instant's offset is written with a colon, as in +07:00"

	if LOCAL_DATE_TIME.fullmatch(text) is None:
		return f'Invalid instant “{abbreviate(text)}”. {INSTANT_FORMAT}'

	if offset is not None:
		# The instant has the offset it was meant in, so it is not a local time to write as a string.
		reason = "An instant's offset follows its time directly, without a space"
		return f'{reason}, as in {abbreviate(f"{text}{offset}")}' if is_valid_value(f'{text}{offset}') else reason

	if not is_whole_value:
		return 'An instant needs an offset: Z or ±HH:MM'

	return f"An instant needs an offset: Z or ±HH:MM. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '{abbreviate(text)}', or add the offset it was meant in"


def describe_space_separated_instant(date_text: str, time: str, *, is_whole_value: bool) -> str:
	"""A date and a time with a space between them, where an instant has a "T". Following the example must not turn a local time into UTC silently, so a time without an offset is described as what it is. An instant is only shown when it is valid, so a date that does not exist, a time out of range, or an instant outside the years 0001 to 9999 in UTC gets the general format instead."""
	separator = 'The date and time separator in an instant is an uppercase “T”, not a space'
	instant = f'{date_text}T{time}'

	if len(instant) > MAX_DIAGNOSED_LENGTH:
		return f'{separator}. {INSTANT_FORMAT}'

	if is_valid_value(instant):
		return f'{separator}, as in {abbreviate(instant)}'

	if is_whole_value and LOCAL_DATE_TIME.fullmatch(instant) is not None and is_valid_value(f'{instant}Z'):
		return f"{separator}, and an instant needs the offset it was meant in, as in {abbreviate(instant)}Z for UTC. A date and time without an offset is a local time, which is not an instant, so write it as a string, as in '{abbreviate(f'{date_text} {time}')}'"

	return f'{separator}. {INSTANT_FORMAT}'
