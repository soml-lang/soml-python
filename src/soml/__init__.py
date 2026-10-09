"""
Read and write SOML, a config format for humans.

The API is the one of `json` and `tomllib`: `loads()` and `load()` read a document, and `dumps()` and `dump()` write one, in the order of the input, or in canonical form with `canonical=True`. `format()` normalizes the layout of a document and keeps its comments. `soml.tree` reads a document into a syntax tree that keeps comments and spelling, so that a tool can change a value and write the file back.
"""

from ._parser import Document, SOMLDecodeError, Value, parse
from ._writer import Serializable, dumps

# Not imported at runtime, so that importing the package stays fast. Annotations are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Mapping, Sequence
	from io import Reader, Writer
	from types import ModuleType

	from . import tree


__all__ = (
	'Document',
	'SOMLDecodeError',
	'Serializable',
	'Value',
	'dump',
	'dumps',
	'format',
	'load',
	'loads',
	'tree',
)


def loads(text: str | bytes | bytearray, /) -> Document:
	"""
	Parse a document.

	`text` is a `str`, or UTF-8 `bytes` or `bytearray`. Returns a `dict` or a `list`, because a document is always an object or an array.

	An int is an `int` and a float is a `float`, so `3` and `3.0` keep their types. A date is a `datetime.date`, an instant is a `datetime` in UTC, and a duration is a `timedelta`.

	Raises `SOMLDecodeError` when the document is not valid. That includes an instant or a duration with a part finer than a microsecond, which `datetime` and `timedelta` cannot hold.

	>>> loads("name: 'api'\\nreplicas: 3")
	{'name': 'api', 'replicas': 3}
	"""
	return parse(text)


def _refuse_text_file(file: object, mode: str) -> None:
	"""A runtime check, for callers that are not type-checked, because a text file is an easy mistake, and its own error does not say what to do."""
	# Imported here, because Python 3.14 does not load `io` at startup, and most programs never call `load()` or `dump()`.
	from io import TextIOBase  # noqa: PLC0415

	if isinstance(file, TextIOBase):
		raise TypeError(f"File must be opened in binary mode, e.g. use `open('config.soml', '{mode}')`")


def load(file: Reader[bytes], /) -> Document:
	"""
	Parse a document from a file opened in binary mode, like `open('config.soml', 'rb')`.

	Raises `SOMLDecodeError` when the document is not valid, and `TypeError` when the file is opened in text mode.
	"""
	# Before reading, because a text file decodes while it reads, and its own error, such as a `UnicodeDecodeError`, does not say what to do.
	# Note: Not using `io.TextIOWrapper` because it changes stream ownership and can translate line endings that SOML must reject.
	_refuse_text_file(file, 'rb')
	data = file.read()

	# A runtime check too, for a text file that is not a `TextIOBase`.
	if isinstance(data, str):  # type: ignore[unreachable] # pyright: ignore[reportUnnecessaryIsInstance]
		raise TypeError("File must be opened in binary mode, e.g. use `open('config.soml', 'rb')`")

	return parse(data)


def dump(value: Mapping[str, Serializable] | Sequence[Serializable], file: Writer[bytes], /, *, canonical: bool = False) -> None:
	"""
	Write a `dict` or a `list` to a file opened in binary mode, like `open('config.soml', 'wb')`, as UTF-8. The text is the one `dumps()` returns, so members keep the order of the input, or are sorted by key with `canonical=True`.

	Raises the same errors as `dumps()`, and nothing is written when it does. Raises `TypeError` when the file is opened in text mode. The stream must complete the write, as buffered binary files do; an incomplete write raises `OSError`.
	"""
	_refuse_text_file(file, 'wb')
	data = dumps(value, canonical=canonical).encode()
	if file.write(data) != len(data):
		raise OSError('The binary stream reported an incomplete write; use a buffered binary file')


def format(text: str | bytes | bytearray, /) -> str:  # noqa: A001 # The name of the function in the JS reference implementation.
	"""
	Format a document: normalize its layout and keep everything else the author wrote. Returns the formatted document, ending with one line feed.

	The layout follows the formatter in the spec: one tab per level, every member and item on its own line with no commas, and no trailing whitespace or runs of blank lines. An object or an array whose brackets are on one line stays on one line, as in `ports: [80, 443]`, with a comma and a space between its members or items. To give a one-line container one member or item per line, put a line break anywhere inside it. Comments, member order, block strings, and the spelling of every value stay as they are, so the value never changes. The one change inside a block comment is the same layout rule: trailing whitespace is removed, and runs of blank lines collapse to one.

	Raises the same `SOMLDecodeError` as `loads()`, and `TypeError` when `text` is not a `str` or bytes. The exception is an instant or a duration finer than a microsecond: `format()` accepts it, because it keeps the text of every value and never converts it to a `datetime` or a `timedelta`.

	>>> print(format('pool: {min: 2,   max: 16,} # Connections'), end='')
	pool: {min: 2, max: 16} # Connections
	>>> print(format('pool: {\\nmin: 2, max: 16}'), end='')
	pool: {
		min: 2
		max: 16
	}
	"""
	# Imported here, because the formatter needs `soml.tree`, which most programs never load.
	from ._format import format_document  # noqa: PLC0415

	return format_document(text)


if not TYPE_CHECKING:
	# Hidden from type checkers, which know `soml.tree` from the import above, so that a typo such as `soml.lods` is still reported as a missing name.
	def __getattr__(name: str) -> ModuleType:
		# `soml.tree` is imported when it is first used, because most programs only read and write values, and the tree takes most of the import time.
		if name == 'tree':
			import importlib  # noqa: PLC0415

			return importlib.import_module('.tree', __name__)

		raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__() -> list[str]:
	return sorted({*globals(), 'tree'})
