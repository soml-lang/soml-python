"""
A syntax tree that keeps everything the author wrote.

`parse()` reads a document into nodes, a program changes them, by path with `DocumentNode.get()`, `set()`, and `remove()`, or on the nodes, and `unparse()` prints them back. Whatever was not changed is copied from the source byte for byte, comments included, and only what changed is printed fresh, so a tool can change one value in a config file without touching the rest of it.

>>> from soml import tree
>>> document = tree.parse("name: 'api'  # The service.\\nport: 0x1F90\\n")
>>> document.get('port').value = 9090
>>> print(tree.unparse(document), end='')
name: 'api'  # The service.
port: 0x2382
"""

# The nodes' `_origin` and `_snapshot()` are private to this module, not to their class.
# pyright: reportPrivateUsage=false

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence  # At runtime, so that `typing.get_type_hints()` works on the overloads of `from_value()`.
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from operator import index as exact_int
from operator import is_
from typing import Literal, NamedTuple, cast, overload

from ._parser import (
	BARE_KEY,
	MAX_DEPTH,
	RADICES,
	Parser,
	SOMLDecodeError,
	Value,
	abbreviate,
	check_characters,
	decode,
	describe_key,
	is_bare_key,
	sanitize,
	skip_spaces,
	skip_spaces_back,
	without_comments,
)
from ._parser import parse as parse_document
from ._writer import (
	RADIX_FORMATS,
	Serializable,
	check_document_type,
	check_float,
	check_integer,
	check_string,
	checked_members,
	enter_container,
	exact_date,
	exact_duration,
	exact_float,
	exact_instant,
	exact_str,
	format_duration,
	format_escaped_string,
	format_float,
	format_instant,
	format_key,
	format_string,
	is_array,
	is_object,
	raise_unsupported_type,
)

# Not imported at runtime, so that importing the package stays fast. Annotations are evaluated only when something asks for them.
TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable, Iterator

# The native callback for the position cache, typed once for string matches.
_match_end = re.Match[str].end

__all__ = (
	'ArrayNode',
	'BooleanNode',
	'Comment',
	'DateNode',
	'DocumentNode',
	'DurationNode',
	'FloatNode',
	'InstantNode',
	'IntegerNode',
	'KeyNode',
	'MemberNode',
	'Node',
	'NullNode',
	'ObjectNode',
	'Path',
	'StringNode',
	'Token',
	'ValueNode',
	'from_value',
	'parse',
	'unparse',
	'walk',
)


@dataclass(slots=True, eq=False)
class _Origin:
	"""Where a node came from: its range in the source, a snapshot of its fields when it was parsed, and the source text around its children."""

	source: str
	range: tuple[int, int]
	snapshot: tuple[object, ...]
	children: tuple[_Node, ...] = ()
	# For a container: the text after the opening delimiter, then the text before and after each child, then the text before the closing delimiter. For a member: the text between the key and the value, which holds the `:`. For a document: the text before and after the body.
	trivia: tuple[str, ...] = ()
	# For a container: whether the text after each child holds its comma.
	commas: tuple[bool, ...] = ()


@dataclass(slots=True, eq=False)
class _Node:
	range: tuple[int, int] | None = field(default=None, kw_only=True)
	"""The start and end of the node as indices into the source, so `source[start:end]` is its text. `None` for a node that a program made."""

	_origin: _Origin | None = field(default=None, init=False, repr=False)

	# The node that `DocumentNode.set()` replaced with this one, whose place in its container, with its comments and layout, this one takes.
	_replaces: _Node | None = field(default=None, init=False, repr=False)

	def _snapshot(self) -> tuple[object, ...]:
		"""The fields that decide whether the node changed. Children are compared by identity against the origin, because a child that changed decides that for itself."""
		raise NotImplementedError

	def _children(self) -> Sequence[Node]:
		"""The structural children in source order, without copying a container's list."""
		return ()

	def _is_unchanged(self, changed: set[int]) -> bool:
		"""Whether this node and its original children still match the source, without preparing layout for an unchanged subtree."""
		origin = self._origin
		if origin is None:
			return False

		if not origin.children:
			return self._snapshot() == origin.snapshot

		identity = id(self)
		if identity in changed:
			return False

		if isinstance(self, MemberNode):
			children_match = self.key is origin.children[0] and self.value is origin.children[1] and not self.comments
		else:
			children = self._children()
			# Note: Not using tuple equality because a node subclass can override equality; unchanged children must be the same objects.
			children_match = len(children) == len(origin.children) and all(map(is_, children, origin.children, strict=True))

		unchanged = self._snapshot() == origin.snapshot and children_match and all(child._is_unchanged(changed) for child in origin.children)

		if not unchanged:
			changed.add(identity)

		return unchanged


type ValueNode = ObjectNode | ArrayNode | StringNode | IntegerNode | FloatNode | BooleanNode | NullNode | InstantNode | DateNode | DurationNode
"""A node that is a value."""

type Node = DocumentNode | MemberNode | KeyNode | ValueNode
"""Any node in a tree."""


@dataclass(slots=True, eq=False)
class StringNode(_Node):
	"""A string, such as `'api'`, `"a\\n"`, or a block string."""

	value: str
	"""The decoded string."""

	style: Literal['literal', 'escaped'] = 'literal'
	"""`'literal'` for `'...'`, or `'escaped'` for `"..."`. A changed `'literal'` string is written as `"..."` when it needs an escape."""

	block: bool = False
	"""Whether it is a block string. A changed block string is written on one line, on the line of its key when it began on the line after it."""

	def _checked_value(self) -> str:
		"""The exact string, shared by conversion and printing before either validates its contents."""
		if not isinstance(self.value, str):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A StringNode holds a str, not {type(self.value).__name__!r}')

		return exact_str(self.value)

	def to_value(self) -> str:
		string = self._checked_value()
		check_string(string, 'string')
		return string

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value, self.style, self.block)


@dataclass(slots=True, eq=False)
class IntegerNode(_Node):
	"""An int, such as `8080` or `0x1F90`."""

	value: int

	radix: Literal[2, 8, 10, 16] = 10
	"""The radix it is written in. A changed negative value is written in decimal, because an alternate radix takes no sign."""

	def to_value(self) -> int:
		if isinstance(self.value, bool) or not isinstance(self.value, int):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'An IntegerNode holds an int, not {type(self.value).__name__!r}')

		integer = exact_int(self.value)
		check_integer(integer)
		return integer

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value, type(self.radix), self.radix)


@dataclass(slots=True, eq=False)
class FloatNode(_Node):
	"""A float, such as `1.5`, `1e21`, or `infinity`."""

	value: float

	def to_value(self) -> float:
		# An int is allowed, because a type checker accepts an int for a float, and `exact_float()` converts it. A bool is not, because it is not meant as a number.
		if isinstance(self.value, bool) or not isinstance(self.value, int | float):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A FloatNode holds a float, not {type(self.value).__name__!r}')

		value = exact_float(self.value)
		check_float(value)
		# Negative zero is the same value as zero, and `loads()` reads both as `0.0`. Only a node a program made can hold `-0.0`.
		return value or 0.0

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value)


@dataclass(slots=True, eq=False)
class BooleanNode(_Node):
	"""`true` or `false`."""

	value: bool

	def to_value(self) -> bool:
		if type(self.value) is not bool:
			raise TypeError(f'A BooleanNode holds a bool, not {type(self.value).__name__!r}')

		return self.value

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value)


@dataclass(slots=True, eq=False)
class NullNode(_Node):
	"""`null`."""

	def to_value(self) -> None:
		return None

	def _snapshot(self) -> tuple[object, ...]:
		return ()


@dataclass(slots=True, eq=False)
class InstantNode(_Node):
	"""An instant, such as `2026-09-19T14:00:00Z`."""

	value: datetime
	"""The instant. When parsed, it is in UTC, and the source keeps the offset it was written with."""

	def _checked_value(self) -> datetime:
		"""The instant, shared by conversion and printing before either normalizes its time zone."""
		if not isinstance(self.value, datetime):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'An InstantNode holds a datetime, not {type(self.value).__name__!r}')

		return self.value

	def to_value(self) -> datetime:
		return exact_instant(self._checked_value())

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value)


@dataclass(slots=True, eq=False)
class DateNode(_Node):
	"""A calendar date, such as `2026-11-01`, with no time or timezone."""

	value: date

	def to_value(self) -> date:
		if isinstance(self.value, datetime) or not isinstance(self.value, date):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A DateNode holds a date, not {type(self.value).__name__!r}')

		return exact_date(self.value)

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value)


@dataclass(slots=True, eq=False)
class DurationNode(_Node):
	"""A duration, such as `1h30m`."""

	value: timedelta

	def _checked_value(self) -> timedelta:
		"""The duration, shared by conversion and printing before either checks its range."""
		if not isinstance(self.value, timedelta):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A DurationNode holds a timedelta, not {type(self.value).__name__!r}')

		return self.value

	def to_value(self) -> timedelta:
		return exact_duration(self._checked_value())

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value)


@dataclass(slots=True, eq=False)
class KeyNode(_Node):
	"""The key of a member, such as `name` or `'the name'`."""

	value: str
	"""The decoded key."""

	style: Literal['bare', 'literal', 'escaped'] = 'bare'
	"""How it is written. A changed `'bare'` key is quoted when it is not a valid bare key."""

	def _checked_value(self) -> str:
		"""The exact key, shared by conversion and printing before either validates its contents."""
		if not isinstance(self.value, str):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A KeyNode holds a str, not {type(self.value).__name__!r}')

		return exact_str(self.value)

	def _snapshot(self) -> tuple[object, ...]:
		return (type(self.value), self.value, self.style)


@dataclass(slots=True, eq=False)
class MemberNode(_Node):
	"""A member of an object: a key and its value."""

	key: KeyNode
	value: ValueNode

	comments: list[str] = field(default_factory=cast('type[list[str]]', list), kw_only=True)
	"""
	Comment lines that `unparse()` writes above a member that a program made, each as `# text`. A comment cannot hold a line break.

	Always empty for a member from `parse()`, whose comments are in `DocumentNode.comments`, and `unparse()` raises `ValueError` when it is not, because the comments of the source cannot be edited. Also when the member goes on the line of the text before it, such as `{a: 1}`, where a comment line would split that line.
	"""

	def _snapshot(self) -> tuple[object, ...]:
		return ()

	def _children(self) -> Sequence[Node]:
		return (self.key, self.value)


@dataclass(slots=True, eq=False)
class ObjectNode(_Node):
	"""An object, written with braces, or the top-level object without them."""

	members: list[MemberNode] = field(default_factory=cast('type[list[MemberNode]]', list))

	braced: bool = True
	"""`False` only for a top-level object written without braces. Changing it prints the object fresh, so the comments between and after its members go."""

	def _member_index(self, key: str) -> int | None:
		"""Find a member once, so removal can delete it without searching again."""
		# Note: Not using `next()` with a generator because it was slower for early, late, and missing keys.
		for index, member in enumerate(self.members):
			value = member.key.value
			if type(value) is not str:
				value = exact_str(value)
			if value == key:
				return index

		return None

	def to_value(self, _depth: int = 1, _ancestors: set[int] | None = None) -> dict[str, Value]:
		"""The plain value, as `loads()` returns it."""
		_ancestors = _tree_ancestors(self, _depth, _ancestors)

		try:
			result: dict[str, Value] = {}

			for member in self.members:
				if not isinstance(member, MemberNode):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
					raise TypeError(f'Expected a node, not {type(member).__name__!r}. Use `from_value()` to make a node from a value')

				if not isinstance(member.key, KeyNode):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
					raise TypeError(f'Expected a node, not {type(member.key).__name__!r}. Use `from_value()` to make a node from a value')

				key = member.key._checked_value()
				check_string(key, 'key')

				# The parser's rule for duplicate keys, so a tree with a duplicate key is refused here as `unparse()` refuses it.
				if key in result:
					raise ValueError(sanitize(f'Duplicate key {describe_key(key)}'))

				child = _value_node(member.value)

				result[key] = child.to_value(_depth + 1, _ancestors) if isinstance(child, _CONTAINER_NODE_TYPES) else child.to_value()

			return result
		finally:
			_ancestors.remove(id(self))

	def _snapshot(self) -> tuple[object, ...]:
		return (self.braced, type(self.braced), len(self.members))

	def _children(self) -> Sequence[Node]:
		return self.members


@dataclass(slots=True, eq=False)
class ArrayNode(_Node):
	"""An array."""

	elements: list[ValueNode] = field(default_factory=cast('type[list[ValueNode]]', list))

	def to_value(self, _depth: int = 1, _ancestors: set[int] | None = None) -> list[Value]:
		_ancestors = _tree_ancestors(self, _depth, _ancestors)

		try:
			values: list[Value] = []

			for element in self.elements:
				child = _value_node(element)
				values.append(child.to_value(_depth + 1, _ancestors) if isinstance(child, _CONTAINER_NODE_TYPES) else child.to_value())

			return values
		finally:
			_ancestors.remove(id(self))

	def _snapshot(self) -> tuple[object, ...]:
		return (len(self.elements),)

	def _children(self) -> Sequence[Node]:
		return self.elements


def _tree_ancestors(node: ObjectNode | ArrayNode, depth: int, ancestors: set[int] | None) -> set[int]:
	"""Enter a container while enforcing the shared conversion depth and cycle limits."""
	if ancestors is None:
		ancestors = set()

	if depth > MAX_DEPTH or id(node) in ancestors:
		raise ValueError(f'The tree is nested more than {MAX_DEPTH} levels deep, or it is circular')

	ancestors.add(id(node))
	return ancestors


_CONTAINER_NODE_TYPES = (ObjectNode, ArrayNode)
_VALUE_NODE_TYPES = (ObjectNode, ArrayNode, StringNode, IntegerNode, FloatNode, BooleanNode, NullNode, InstantNode, DateNode, DurationNode)


def _value_node(node: object) -> ValueNode:
	"""Reject structural nodes where the tree requires a value."""
	if isinstance(node, _VALUE_NODE_TYPES):
		return node

	kind = 'a value node' if isinstance(node, _Node) else 'a node'
	raise TypeError(f'Expected {kind}, not {type(node).__name__!r}. Use `from_value()` to make a node from a value')


def _check_children(node: ObjectNode | ArrayNode) -> None:
	"""Check children only when rendering a container, after unchanged source has been reused."""
	if isinstance(node, ObjectNode):
		for member in node.members:
			if not isinstance(member, MemberNode):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
				raise TypeError(f'Expected a MemberNode, not {type(member).__name__!r}')
	else:
		for element in node.elements:
			_value_node(element)


class Comment(NamedTuple):
	"""
	A comment. Comments are read-only: `unparse()` does not read `DocumentNode.comments`, so changing them changes nothing that it writes.

	A named tuple, like `Token`, because a document has many of them, and a tuple is faster to make than a dataclass.
	"""

	value: str
	"""The text without its delimiters, so `' The default'` for `# The default`."""

	kind: Literal['line', 'block']
	range: tuple[int, int]


class Token(NamedTuple):
	"""A token, for tools such as linters. `value` is its source text. A `'punctuator'` is one of `{`, `}`, `[`, `]`, `:`, and `,`, and a quoted key is a `'string'` token."""

	value: str
	kind: Literal['punctuator', 'bare_key', 'string', 'integer', 'float', 'keyword', 'instant', 'date', 'duration']
	range: tuple[int, int]


@dataclass(slots=True, eq=False)
class DocumentNode(_Node):
	"""The root of a tree: the document's object or array, and its source, comments, and tokens."""

	body: ObjectNode | ArrayNode

	comments: list[Comment] = field(default_factory=cast('type[list[Comment]]', list))
	"""Every comment, in source order. Read-only."""

	tokens: list[Token] = field(default_factory=cast('type[list[Token]]', list))
	"""Every token, in source order, without comments. Read-only."""

	source: str = ''
	"""The text the document was parsed from."""

	_line_starts: tuple[str, list[int]] | None = field(default=None, init=False, repr=False)

	@classmethod
	def from_value(cls, value: Mapping[str, Serializable] | Sequence[Serializable], /) -> DocumentNode:
		"""
		A new document for a plain value, such as a starter config that a tool writes. It is written as `from_value()` writes a node, which is as `dumps()` writes it: object members keep their order, and a top-level object has no braces. An empty object stays `{}`.

		Raises the same errors as `dumps()`, including `TypeError` for a value that is not an object or an array.

		>>> document = DocumentNode.from_value({'name': 'api', 'tags': ['edge']})
		>>> print(unparse(document), end='')
		name: 'api'
		tags: [
			'edge'
		]
		"""
		# A runtime check too, for callers that are not type-checked, because `from_value()` accepts a scalar. Checked first, as in `dumps()`, so that a scalar such as NaN gets the same error.
		check_document_type(value)

		body = from_value(value)

		if isinstance(body, ObjectNode) and body.members:
			body.braced = False

		return cls(body)

	def position(self, offset: int) -> tuple[int, int]:
		"""The 1-based line and column of an index into `source`, from 0 to its length, as `SOMLDecodeError` counts them."""
		if type(offset) is not int:
			if not isinstance(offset, int):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
				raise TypeError(f'The offset must be an int, not {type(offset).__name__!r}')

			offset = exact_int(offset)

		if not 0 <= offset <= len(self.source):
			raise ValueError(f'The offset {offset} is outside the source, which has {len(self.source)} characters')

		# The start of each line, found once, so that a linter can ask for many positions.
		# Compared by identity, which is cheap, so a program that assigns another `source` gets new line starts.
		# Note: Not using `splitlines()` with `accumulate()` because Unicode line separators are content, not SOML line feeds, and splitting copies every line.
		if self._line_starts is None or self._line_starts[0] is not self.source:
			self._line_starts = (self.source, [0, *map(_match_end, re.finditer('\n', self.source))])

		line_starts = self._line_starts[1]
		line = bisect_right(line_starts, offset)
		return (line, offset - line_starts[line - 1] + 1)

	def get(self, path: Path, /) -> ValueNode | None:
		"""
		The value node at a path, or `None` when no node is there. A path is a key, an array index, or a sequence of keys and indices, such as `('servers', 0, 'port')`.

		Raises `ValueError` for an empty path or a negative index, and `TypeError` for an item of the path that is not a `str` or an `int`, as `set()`, `remove()`, `in`, and `del` do.

		>>> document = parse('postgres: {port: 0x1538}')
		>>> document.get(('postgres', 'port'))
		IntegerNode(range=(17, 23), value=5432, radix=16)
		"""
		return _lookup(self.body, _path_segments(path))

	def __contains__(self, path: Path, /) -> bool:
		"""
		Whether there is a value at a path, so `'port' in document`. The path is as for `get()`, and so are its errors.

		>>> 'postgres' in parse('postgres: {port: 5432}')
		True
		"""
		return self.get(path) is not None

	def __delitem__(self, path: Path, /) -> None:
		"""
		Remove the member or item at a path, as `remove()` does, so `del document['port']`. Unlike `remove()`, which returns `False` when nothing is there, it raises `KeyError` then, as `del` does on a `dict`. Use `remove()` to remove a value that may not be there. It raises the errors of `remove()` for a path that does not fit the document.
		"""
		if not self.remove(path):
			raise KeyError(path)

	def set(self, path: Path, value: Serializable, /) -> None:
		"""
		Set the value at a path, replacing what is there, or adding it. The path is as for `get()`, and the value is a plain value, as for `from_value()`.

		- A replaced value keeps the comments around it.
		- A new member goes after the last member of its object, with that member's layout. It goes on its own line with no comma, because the line break separates it, after the comments that the member before it owns (see `remove()`), so before a comment on a line of its own. When something other than its comma and those comments follows the member before it on that member's line, such as the `}` of `{a: 1}`, it goes on that line after a comma instead, and it has a comma after it when the member before has one. So `{a: 1}` becomes `{a: 1, b: 2}`, and `[1, 2 /* note */]` with a new item becomes `[1, 2 /* note */, 3]`.
		- A missing parent object is made with braces, written on several lines, or on one line in a container that stays on one line.
		- An index equal to the length of an array appends to it. A path below it makes that item, with objects for the keys after it, so `('b', 0, 'c')` in `b: []` adds the item `{c: 1}`.
		- In a container that stays on one line, a new or replaced value is written on one line too, so `[1, 2]` with a new item `{'a': 3}` becomes `[1, 2, {a: 3}]`. A container stays on one line when its brackets are on one line with something other than spaces and tabs between them, as in `[1, 2]` and `[/* note */]`, or when it is inside a container that stays on one line. A new member or item in a container with no members or items goes on a line of its own, as in an empty `[]` or `{}`, unless the container stays on one line: then it goes before the closing bracket, so `[/* note */]` becomes `[/* note */ 1]`, and `a: [1, []]` becomes `a: [1, [2]]`.

		Raises `ValueError` when the path does not fit the document: it goes through a value that is not an object or an array, uses a key on an array or an index on an object, has an index past the end of an array, or has an index below a value that does not exist, which would need an array that is not there. Also when the result would not be a valid document, such as one nested more than 100 levels deep, and the errors of `dumps()` for a value that cannot be written. The document is unchanged after an error.

		The result is checked by reading the whole document back, so a change takes time in proportion to the size of the document.

		>>> document = parse("port: 0x1F90 # The default.\\nserver: {host: 'a'}\\n")
		>>> document.set('port', 8080)
		>>> document.set(('server', 'port'), 443)
		>>> document.set('replicas', 3)
		>>> print(unparse(document), end='')
		port: 8080 # The default.
		server: {host: 'a', port: 443}
		replicas: 3
		"""
		segments = _path_segments(path)
		if len(segments) > MAX_DEPTH:
			raise ValueError(f'The tree is nested more than {MAX_DEPTH} levels deep, or it is circular')

		undo = _set_in(self.body, segments, from_value(value))

		try:
			unparse(self)
		except Exception:
			undo()
			raise

	def remove(self, path: Path, /) -> bool:
		"""
		Remove the member or item at a path, with the comments it owns and the comma after it. A member or item owns the comments that `format()` keeps with it, as the "Editing" rules of the spec say: the comments after it on its line, also after its comma when nothing else follows there, and the block comments before it on its line, after the comma or bracket before it. So removing `2` from `[1, /* note */ 2]` gives `[1]`. A comment on a line of its own belongs to no member or item, so the comment lines above it stay. When it is the last one and has no comma, it takes the comma before it instead, when only spaces, tabs, and the comments it owns are between, so `[1, 2]` becomes `[1]`. No other comma changes. Removing the only member of a document without braces leaves `{}` in its place, because a document is never empty. Like a value that `set()` replaces, such an empty object keeps the comments around the member, so `port: 8080 # Note` becomes `{} # Note`.

		Comment ownership and layout refer to the original source throughout a sequence of edits. For example, adding and then removing an item in `[/* note */]` restores `[/* note */]`. To apply each edit to the text produced by the previous one, reparse it with `document = tree.parse(tree.unparse(document))` between edits.

		Removing a value that does not exist changes nothing and is not an error: a missing member, a missing object or array on the way, or an index at or past the end of its array. So removing the same path twice is safe. Returns whether something was removed, so a caller that expected a value can tell that it was missing. `del` raises `KeyError` instead.

		Raises `ValueError` when the path does not fit the document before it leads to nothing: it goes through a value that is not an object or an array, or uses a key on an array or an index on an object. The document is unchanged after an error.

		>>> document = parse("name: 'api' # The service.\\nport: 8080\\n")
		>>> document.remove('name')
		True
		>>> document.remove('name')
		False
		>>> print(unparse(document), end='')
		port: 8080
		"""
		segments = _path_segments(path)
		return _remove_in(self.body, segments)

	def to_value(self) -> dict[str, Value] | list[Value]:
		# Note: Not using `dataclasses.asdict()` because it exposes node structure and metadata instead of the SOML value.
		if not isinstance(self.body, _CONTAINER_NODE_TYPES):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'Expected a node, not {type(self.body).__name__!r}. Use `from_value()` to make a node from a value')

		return self.body.to_value()

	def _snapshot(self) -> tuple[object, ...]:
		return ()

	def _children(self) -> Sequence[Node]:
		return (self.body,)


type Path = str | int | Sequence[str | int]
"""A key, an array index, or a sequence of keys and indices, such as `('servers', 0, 'port')`."""

# The text of soml-js, whose `edit()` takes the same paths.
INVALID_PATH = 'The path must be a non-empty array of keys and array indexes'


def _path_segments(path: Path) -> tuple[str | int, ...]:
	# The common single-key and single-index paths need neither a sequence scan nor normalization.
	if type(path) is str:
		return (path,)

	if type(path) is int:
		if path < 0:
			raise ValueError(INVALID_PATH)
		return (path,)

	# Bytes are a sequence of ints, which would be read as indices.
	if isinstance(path, (bytes, bytearray, memoryview)):
		raise TypeError(INVALID_PATH)

	# A string is one key, and an int is one index. Without this, a string would be read as a sequence of one-character keys.
	if isinstance(path, (str, int)):
		segments: tuple[str | int, ...] = (path,)
	else:
		# Only a sequence holds keys and indices. A dict, set, or generator would otherwise be read as its items.
		if not isinstance(path, Sequence):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(INVALID_PATH)

		try:
			segments = tuple(path)
		except TypeError:
			# A path that is not a sequence, such as `None`.
			raise TypeError(INVALID_PATH) from None

	if not segments:
		raise ValueError(INVALID_PATH)

	has_subclasses = False
	for segment in segments:
		if type(segment) is str:
			continue
		if type(segment) is int:
			if segment < 0:
				raise ValueError(INVALID_PATH)
			continue

		has_subclasses = True
		# `bool` is an `int`, but `True` is not meant as an index.
		if isinstance(segment, bool) or not isinstance(segment, (str, int)):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check too, for callers that are not type-checked.
			raise TypeError(INVALID_PATH)

		if isinstance(segment, int) and exact_int(segment) < 0:
			raise ValueError(INVALID_PATH)

	# Keep exact tuple paths without another allocation, especially a huge path rejected before it is used.
	if has_subclasses:
		return tuple(exact_int(segment) if isinstance(segment, int) else exact_str(segment) for segment in segments)

	return segments


def _format_path(path: tuple[str | int, ...]) -> str:
	"""
	The path as soml-js writes it in an error message, such as `servers[0]."the name"`: each key bare when it can be and otherwise as a JSON string, each index in brackets, cut to 200 characters, with invisible characters and those that a terminal acts on written as escapes. A lone surrogate is written as `JSON.stringify()` writes it, such as `\\ud800`.
	"""
	import unicodedata  # noqa: PLC0415 # Only needed for an error.

	parts: list[str] = []
	length = 0

	for index, segment in enumerate(path):
		part = f'[{segment}]' if isinstance(segment, int) else f'{"." if index else ""}{describe_key(segment)}'
		parts.append(part)
		length += len(part)
		if length > 200:
			break

	text = abbreviate(''.join(parts), 200)

	def escape(character: str) -> str:
		match unicodedata.category(character):
			case 'Cc' | 'Cf' | 'Zl' | 'Zp':
				return f'\\u{{{ord(character):x}}}'
			case 'Cs':
				return f'\\u{ord(character):04x}'
			case _:
				return character

	return ''.join(map(escape, text))


def _describe_parent(path: tuple[str | int, ...], count: int) -> str:
	"""The value that holds the item of `path` at `count`, as soml-js describes it: the path of its first `count` items, or “the document” when `count` is zero."""
	return _format_path(path[:count]) if count else 'the document'


def _lookup(node: ValueNode, path: tuple[str | int, ...]) -> ValueNode | None:
	"""The value node at a path in a node, or `None` when no node is there."""
	for step in path:
		match node, step:
			case ObjectNode(members=members), str():
				member_index = node._member_index(step)

				if member_index is None:
					return None

				node = members[member_index].value
			case ArrayNode(elements=elements), int() if step < len(elements):
				node = elements[step]
			case _:
				return None

	return node


def _key_node(key: str) -> KeyNode:
	return KeyNode(key, 'bare' if is_bare_key(key) else 'literal')


def _with_missing_objects(value: ValueNode, path: tuple[str | int, ...], offset: int) -> ValueNode:
	"""Wrap a new value in the missing objects named by the rest of its path."""
	# Note: Not using `itertools.islice()` because the extra iterator did not materially improve these depth-limited paths.
	for key in reversed(path[offset + 1 :]):
		if isinstance(key, int):
			raise _missing_array_error(path)

		value = ObjectNode([MemberNode(_key_node(key), value)])

	return value


def _set_in(node: ValueNode, path: tuple[str | int, ...], value: ValueNode) -> Callable[[], object]:
	"""Sets `value` at `path` inside `node`, and returns a function that undoes it."""
	last = len(path) - 1
	for offset, step in enumerate(path):
		is_last = offset == last

		match node:
			case ObjectNode(members=members):
				if isinstance(step, int):
					raise _path_error(path, offset, 'is an object, so it needs a key, not an index')

				member_index = node._member_index(step)

				if member_index is not None:
					member = members[member_index]
					if not is_last:
						node = member.value
						continue

					old = member.value
					member.value = value
					return partial(setattr, member, 'value', old)

				# The path starts with a key, so an index in it is further on, below the new member, where it would need an array that is not there.
				# A missing parent object is made with braces, as canonical form writes it, as in soml-js.
				new_member = MemberNode(_key_node(step), _with_missing_objects(value, path, offset))
				members.append(new_member)
				return members.pop
			case ArrayNode(elements=elements):
				if not isinstance(step, int):
					raise _path_error(path, offset, 'is an array, so it needs an index, not a key')

				if step > len(elements):
					owner = f'the array at {_describe_parent(path, offset)}' if offset else 'the document'
					raise ValueError(f'Cannot edit {_format_path(path)}, because {owner} has {len(elements)} item{"" if len(elements) == 1 else "s"}. Add an item at index {len(elements)}')

				if step == len(elements):
					# A path below a new item makes it, with objects for the keys of the rest of the path, but an index there would need an array that is not there.
					elements.append(_with_missing_objects(value, path, offset))
					# `set()` runs the undo before anything else changes the array, so the new item is still the last one.
					return elements.pop

				if is_last:
					old_element = elements[step]
					# The new item takes the place of the old one, with its comments and layout. A node that `set()` made earlier passes on the node from the source that it replaced.
					value._replaces = old_element if old_element._origin is not None or old_element._replaces is None else old_element._replaces
					elements[step] = value
					return partial(elements.__setitem__, step, old_element)

				node = elements[step]
			case _:
				raise _path_error(path, offset, 'is not an object or an array')

	raise AssertionError('Unreachable, because the path is non-empty')


def _remove_in(node: ValueNode, path: tuple[str | int, ...]) -> bool:
	"""Remove a path by descending once without copying its remaining segments."""
	last = len(path) - 1
	for offset, step in enumerate(path):
		is_last = offset == last

		match node:
			case ObjectNode(members=members):
				if isinstance(step, int):
					raise _path_error(path, offset, 'is an object, so it needs a key, not an index')

				member_index = node._member_index(step)

				# Removing a member that is not there changes nothing, and so does removing something below it.
				if member_index is None:
					return False

				if not is_last:
					node = members[member_index].value
					continue

				del members[member_index]
				return True
			case ArrayNode(elements=elements):
				if not isinstance(step, int):
					raise _path_error(path, offset, 'is an array, so it needs an index, not a key')

				# Removing an item that is not there changes nothing, at the end of the array or past it, and so does removing something below it.
				if step >= len(elements):
					return False

				if not is_last:
					node = elements[step]
					continue

				del elements[step]
				return True
			case _:
				raise _path_error(path, offset, 'is not an object or an array')

	raise AssertionError('Unreachable, because the path is non-empty')


def _path_error(path: tuple[str | int, ...], count: int, reason: str) -> ValueError:
	"""The error for a path that does not fit the document, in the words of soml-js `edit()`, which sets and removes: `reason` is about the value that the first `count` items of the path lead to. It is a `ValueError`, even for a key on an array, because the path does not fit the document, which is a value error."""
	return ValueError(f'Cannot edit {_format_path(path)}, because {_describe_parent(path, count)} {reason}')


def _missing_array_error(path: tuple[str | int, ...]) -> ValueError:
	"""The error for an index below a value that `set()` would make, which needs an array that is not there. It names the last such index, as soml-js does."""
	count = next(index for index in range(len(path) - 1, -1, -1) if isinstance(path[index], int))
	return _path_error(path, count, f'does not exist, so it has no index {path[count]}')


def walk(node: Node, /) -> Iterator[Node]:
	"""
	Yield the node and every node inside it, in source order: a node comes before its children, and a member's key before its value. It is like `ast.walk()`, but in source order, for tools such as linters.

	It does not yield comments and tokens, which are in `DocumentNode.comments` and `DocumentNode.tokens`.

	>>> [type(node).__name__ for node in walk(parse('a: [1]'))]
	['DocumentNode', 'ObjectNode', 'MemberNode', 'KeyNode', 'ArrayNode', 'IntegerNode']
	"""
	# Note: Not using recursion because a deep tree could reach the recursion limit. Each container is tracked while its children are walked, so that a circular tree raises instead of yielding forever.
	# Iterators keep the stack proportional to nesting depth, even for an array with millions of elements.
	stack: list[tuple[Node | None, Iterator[Node]]] = [(None, iter((node,)))]
	active: set[int] = set()

	while stack:
		try:
			current = next(stack[-1][1])
		except StopIteration:
			parent, _ = stack.pop()
			if parent is not None:
				active.remove(id(parent))
			continue

		if not isinstance(current, _Node):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'Expected a node, not {type(current).__name__!r}. Use `from_value()` to make a node from a value')

		if id(current) in active:
			raise ValueError(f'The tree is nested more than {MAX_DEPTH} levels deep, or it is circular')

		yield current

		children = current._children()
		if children:
			active.add(id(current))
			stack.append((current, iter(children)))


def parse(text: str | bytes | bytearray, /) -> DocumentNode:
	"""
	Parse a document into a syntax tree, for tools that change a document or check how it is written.

	Raises the same `SOMLDecodeError` as `loads()` for the same input.
	"""
	return _parse(text, truncates_nanoseconds=False)


def _parse(text: str | bytes | bytearray, *, truncates_nanoseconds: bool) -> DocumentNode:
	"""`parse()`, and for the formatter, which reads only the layout, also a document with an instant or a duration finer than a microsecond, whose node then holds a truncated value."""
	source = decode(text)
	# Validates the whole document with the same parser as `loads()`, so the builder can assume valid input, and the two cannot disagree.
	check_characters(source)
	reader = Parser(source, truncates_nanoseconds=truncates_nanoseconds)
	reader.parse_document()
	return _Builder(reader).build()


def unparse(document: DocumentNode, /) -> str:
	"""
	Print a tree as text.

	A tree from `parse()` that was not changed prints its source exactly. A node that was not changed prints its source text, and a node that changed, or that a program made, is printed fresh. The output is always a valid document: a tree that does not make one, for example because of a duplicate key, raises `ValueError`.
	"""
	if not isinstance(document, DocumentNode):  # pyright: ignore[reportUnnecessaryIsInstance]
		raise TypeError(f'Expected a DocumentNode, not {type(document).__name__!r}. Wrap a node in `DocumentNode()` to print it')

	text = _Printer().document_text(document)

	# An unchanged tree prints its source, which `parse()` already checked.
	if document._origin is not None and text is document._origin.source:
		return text

	try:
		parse_document(text)
	except SOMLDecodeError as error:
		raise ValueError(f'The tree does not make a valid document: {error.msg}') from None

	return text


# `bool` is an `int` and `str` is a `Sequence`, so those overloads overlap. The narrower one comes first, so it is the one that applies, as in typeshed.
@overload
def from_value(value: bool, /) -> BooleanNode: ...  # type: ignore[overload-overlap] # pyright: ignore[reportOverlappingOverload] # noqa: FBT001 # A value, not a flag.
@overload
def from_value(value: int, /) -> IntegerNode: ...
@overload
def from_value(value: float, /) -> FloatNode: ...
@overload
def from_value(value: str, /) -> StringNode: ...  # type: ignore[overload-overlap] # pyright: ignore[reportOverlappingOverload]
@overload
def from_value(value: None, /) -> NullNode: ...
@overload
def from_value(value: datetime, /) -> InstantNode: ...  # type: ignore[overload-overlap] # pyright: ignore[reportOverlappingOverload] # datetime subclasses date.
@overload
def from_value(value: date, /) -> DateNode: ...
@overload
def from_value(value: timedelta, /) -> DurationNode: ...
@overload
def from_value(value: Mapping[str, Serializable], /) -> ObjectNode: ...
@overload
def from_value(value: Sequence[Serializable], /) -> ArrayNode: ...
def from_value(value: Serializable, /) -> ValueNode:
	"""
	A node for a plain value, written the way `dumps()` writes it, and object members keep their order. Raises the same errors as `dumps()` for a value it cannot write. A scalar is fine, because a node is not a document.

	>>> from_value({'port': 8080})
	ObjectNode(range=None, members=[MemberNode(range=None, key=KeyNode(range=None, value='port', style='bare'), value=IntegerNode(range=None, value=8080, radix=10), comments=[])], braced=True)
	"""
	return _node_from_value(value, 1, set())


def _node_from_value(value: object, depth: int, ancestors: set[int]) -> ValueNode:
	# Validates the value and its type, with the errors of `dumps()`.
	match value:
		case bool():
			return BooleanNode(value)
		case None:
			return NullNode()
		case str():
			string = exact_str(value)
			check_string(string, 'string')
			return StringNode(string)
		case int():
			integer = exact_int(value)
			check_integer(integer)
			return IntegerNode(integer)
		case float():
			number = exact_float(value)
			check_float(number)
			return FloatNode(number)
		case datetime():
			return InstantNode(exact_instant(value))
		case date():
			return DateNode(exact_date(value))
		case timedelta():
			return DurationNode(exact_duration(value))
		case _:
			pass

	if is_object(value):
		enter_container(value, depth, ancestors)
		members: list[MemberNode] = []

		for key, member in checked_members(value):
			# Validates the key with the errors of `dumps()`.
			check_string(key, 'key')
			members.append(MemberNode(_key_node(key), _node_from_value(member, depth + 1, ancestors)))

		node: ValueNode = ObjectNode(members)
	elif is_array(value):
		enter_container(value, depth, ancestors)
		node = ArrayNode([_node_from_value(item, depth + 1, ancestors) for item in value])
	else:
		raise_unsupported_type(value)

	ancestors.remove(id(value))
	return node


def _mark[NodeType: _Node](node: NodeType, source: str, span: tuple[int, int], *, children: tuple[_Node, ...] = (), trivia: tuple[str, ...] = (), commas: tuple[bool, ...] = ()) -> NodeType:
	"""Records where a parsed node came from, so the printer can tell whether it changed."""
	origin = _Origin(source, span, node._snapshot(), children, trivia, commas)
	node.range = origin.range
	node._origin = origin
	return node


class _Builder:
	"""
	Walks a document that is already known to be valid, and makes the nodes. Each scalar is read by the parser itself, so the tree and `loads()` cannot disagree about a value.
	"""

	__slots__ = ('checked_comments_end', 'comments', 'index', 'length', 'padded', 'reader', 'source', 'tokens')

	def __init__(self, reader: Parser) -> None:
		source = reader.source
		self.source = source
		# The source with a NUL after it, which a valid document cannot hold, so that the current character can be read without a bounds check.
		self.length = len(source)
		self.index = 0
		self.reader = reader
		self.padded = reader.padded
		# The end of the last run of block comments that was found to end its line, so that its comments are not checked again.
		self.checked_comments_end = 0
		self.tokens: list[Token] = []
		self.comments: list[Comment] = []

	def character(self) -> str:
		"""The character at the index, or a NUL past the end."""
		return self.padded[self.index]

	def build(self) -> DocumentNode:
		self.skip_trivia()
		body: ObjectNode | ArrayNode
		content_start = self.index

		match self.character():
			case '{':
				body = self.object()
			case '[':
				body = self.array()
			case _:
				# The block comments before the first member on its line are its own, as in `/* note */ a: 1`, so they are not part of the text before the body.
				content_start = self.owned_comments_start(self.index)
				body = self.bare_object(content_start)

		# A brace-less object keeps the text after its last member as its own, so the text after the body is empty.
		self.skip_trivia()
		end = (body.range or (0, 0))[1]
		prefix = self.source[:content_start]
		suffix = '' if isinstance(body, ObjectNode) and not body.braced else self.source[end:]
		document = DocumentNode(body, self.comments, self.tokens, self.source)
		return _mark(document, self.source, (0, self.length), children=(body,), trivia=(prefix, suffix))

	def punctuator(self) -> None:
		self.tokens.append(Token(self.source[self.index], 'punctuator', (self.index, self.index + 1)))
		self.index += 1

	def skip_trivia(self, *, line_only: bool = False, is_after_comma: bool = False) -> None:
		"""
		Skips spaces, tabs, line breaks, and comments, and records the comments.

		With `line_only`, it reads the rest of a line for the child before it, so it stops after the first line break, and before a block comment that something other than a comment follows on its line, such as the `/* c */` in `[1, /* c */ 2]`, which belongs to the child after it. With `is_after_comma`, it also stops before a block comment that a closing bracket follows, as in `[1, /* c */]`, because a child owns the comments after its comma only when they end its line, as in the "Editing" rules of the spec.
		"""
		source = self.source
		padded = self.padded

		while True:
			character = padded[self.index]
			start = self.index

			if character in ' \t':
				self.index += 1
				if padded[self.index] in ' \t':
					self.index = skip_spaces(source, self.index)
			elif character == '\n':
				self.index += 1

				if line_only:
					return
			elif character == '#':
				end = source.find('\n', start)
				self.index = self.length if end == -1 else end
				self.comments.append(Comment(source[start + 1 : self.index], 'line', (start, self.index)))
			elif source.startswith('/*', start):
				# A run of block comments is checked once, at its first comment, so a long run takes linear time.
				if line_only and start >= self.checked_comments_end:
					is_followed, self.checked_comments_end = self.check_comment_run(start, is_after_comma=is_after_comma)

					if is_followed:
						return

				self.index = source.index('*/', start + 2) + 2
				self.comments.append(Comment(source[start + 2 : self.index - 2], 'block', (start, self.index)))
			else:
				return

	def check_comment_run(self, start: int, *, is_after_comma: bool) -> tuple[bool, int]:
		"""Whether a child, or a closing bracket after a comma, follows the run of block comments at `start` on the line where it ends, and where the run ends."""
		source = self.source
		index = start
		run_end = start

		while source.startswith('/*', index):
			index = run_end = source.index('*/', index + 2) + 2

			index = skip_spaces(source, index)

		# A closing bracket ends the run's line as much as a line break does, so the run belongs to the child before it, unless a comma is between them.
		return index < self.length and source[index] not in ('\n#' if is_after_comma else '\n#]}'), run_end

	def object(self) -> ObjectNode:
		members, start, end, trivia, commas = self.braced_container('}', self.member)
		return _mark(ObjectNode(members), self.source, (start, end), children=tuple(members), trivia=trivia, commas=commas)

	def array(self) -> ArrayNode:
		elements, start, end, trivia, commas = self.braced_container(']', self.value)
		return _mark(ArrayNode(elements), self.source, (start, end), children=tuple(elements), trivia=trivia, commas=commas)

	def braced_container[Child: _Node](self, closing: str, read_child: Callable[[], Child]) -> tuple[list[Child], int, int, tuple[str, ...], tuple[bool, ...]]:
		"""
		Reads `{...}` or `[...]`. The text around each child is split so that the child owns what is on its own lines: the comments and indentation before it, and its comma and the rest of its last line after it. After its comma, the rest of the line is its own only when it ends the line, so in `[1, /* c */]` the comment is not the item's.
		"""
		source = self.source
		start = self.index
		self.punctuator()
		opening_start = self.index
		self.skip_trivia(line_only=True)
		trivia = [source[opening_start : self.index]]
		children: list[Child] = []
		commas: list[bool] = []
		boundary = self.index

		while True:
			self.skip_trivia()

			if self.character() == closing:
				break

			trivia.append(source[boundary : self.index])
			children.append(read_child())
			child_end = self.index
			comment_count = len(self.comments)
			self.skip_trivia()
			has_comma = self.character() == ','

			if has_comma:
				self.punctuator()
			else:
				# Read again below, so that only the rest of the line goes with this child.
				self.index = child_end
				del self.comments[comment_count:]

			self.skip_trivia(line_only=True, is_after_comma=has_comma)
			trivia.append(source[child_end : self.index])
			commas.append(has_comma)
			boundary = self.index

		trivia.append(source[boundary : self.index])
		self.punctuator()
		return children, start, self.index, tuple(trivia), tuple(commas)

	def owned_comments_start(self, offset: int) -> int:
		"""The start of the block comments that end directly before `offset` on its line, which the member there owns, or `offset` when there are none. They are the last comments read."""
		index = len(self.comments)

		while index and self.comments[index - 1].kind == 'block':
			before = skip_spaces_back(self.source, offset)

			if self.comments[index - 1].range[1] != before:
				break

			index -= 1
			offset = self.comments[index].range[0]

		return offset

	def bare_object(self, content_start: int) -> ObjectNode:
		"""Reads a brace-less top-level object. It owns the text from `content_start`, where the comments that its first member owns begin, to the end of the document."""
		source = self.source
		members: list[MemberNode] = []
		trivia = ['']
		boundary = content_start

		while True:
			trivia.append(source[boundary : self.index])
			members.append(self.member())
			member_end = self.index
			self.skip_trivia(line_only=True)
			trivia.append(source[member_end : self.index])
			boundary = self.index
			self.skip_trivia()

			if self.index >= self.length:
				break

		trivia.append(source[boundary:])
		node = ObjectNode(members, braced=False)
		start = (members[0].range or (0, 0))[0]
		end = (members[-1].range or (0, 0))[1]
		return _mark(node, self.source, (start, end), children=tuple(members), trivia=tuple(trivia), commas=(False,) * len(members))

	def member(self) -> MemberNode:
		key = self.key()
		key_end = self.index
		self.punctuator()
		self.skip_trivia()
		value_start = self.index
		value = self.value()
		start = (key.range or (0, 0))[0]
		return _mark(MemberNode(key, value), self.source, (start, self.index), children=(key, value), trivia=(self.source[key_end:value_start],))

	def key(self) -> KeyNode:
		start = self.index
		character = self.character()

		if character in {"'", '"'}:
			self.reader.index = start
			value = self.reader.parse_literal_string() if character == "'" else self.reader.parse_escaped_string()
			self.index = self.reader.index
			token = Token(self.source[start : self.index], 'string', (start, self.index))
			self.tokens.append(token)
			return _mark(KeyNode(value, 'literal' if character == "'" else 'escaped'), self.source, token.range)

		match = BARE_KEY.match(self.source, start)
		assert match is not None  # noqa: S101 # The document is valid.
		self.index = match.end()
		key = match.group()
		token = Token(key, 'bare_key', (start, self.index))
		self.tokens.append(token)
		return _mark(KeyNode(key), self.source, token.range)

	def value(self) -> ValueNode:
		match self.character():
			case '{':
				return self.object()
			case '[':
				return self.array()
			case _:
				pass

		start = self.index
		self.reader.index = start
		value = self.reader.parse_value(1)
		self.index = self.reader.index
		text = self.source[start : self.index]
		node: ValueNode
		kind: Literal['string', 'integer', 'float', 'keyword', 'instant', 'date', 'duration']

		match value:
			case bool():
				node, kind = BooleanNode(value), 'keyword'
			case None:
				node, kind = NullNode(), 'keyword'
			case str():
				node, kind = StringNode(value, 'literal' if text[0] == "'" else 'escaped', text.startswith(("'''", '"""'))), 'string'
			case int():
				radix = RADICES.get(text[1:2], 10) if text.startswith('0') else 10
				node, kind = IntegerNode(value, radix), 'integer'  # type: ignore[arg-type] # The radix is one of the four.
			case float():
				node, kind = FloatNode(value), 'float'
			case datetime():
				node, kind = InstantNode(value), 'instant'
			case date():
				node, kind = DateNode(value), 'date'
			case timedelta():
				node, kind = DurationNode(value), 'duration'
			case _:
				raise AssertionError('Unreachable, because a container was read above')

		token = Token(text, kind, (start, self.index))
		self.tokens.append(token)
		return _mark(node, self.source, token.range)


def _line_indentation(source: str, offset: int) -> str | None:
	"""The indentation of the line at `offset` when the line has nothing else before `offset`, otherwise `None`. It reads only the spaces and tabs before `offset`, so that it is fast on a long line."""
	start = skip_spaces_back(source, offset)

	return source[start:offset] if start == 0 or source[start - 1] == '\n' else None


def _indentation_at(source: str, offset: int, fallback: str) -> str:
	"""The indentation of the line at `offset` when the line has nothing else before `offset`, otherwise `fallback`."""
	indentation = _line_indentation(source, offset)
	return fallback if indentation is None else indentation


def _comment_lines(text: str) -> str:
	"""
	The comment lines and blank lines at the start of the text before a child, without the line the child starts on. A block comment that spans lines is part of the line it ends on, so it is never cut.
	"""
	return text[: without_comments(text).rfind('\n') + 1]


def _is_blank_line(text: str) -> bool:
	# Note: Not using `str.isspace()` because SOML permits only spaces and tabs on a blank line.
	return not text.strip(' \t')


def _ends_with_blank_line(text: str) -> bool:
	"""Whether `text` ends with a whole blank line, checking only that line rather than a run of them."""
	return text.endswith('\n') and _is_blank_line(text[text.rfind('\n', 0, len(text) - 1) + 1 : -1])


def _blank_run_end(text: str) -> int:
	"""Where the blank lines at the start of `text` end, or 0 when it does not start with one."""
	start = 0

	while (line_end := text.find('\n', start)) != -1 and _is_blank_line(text[start:line_end]):
		start = line_end + 1

	return start


def _join_at_removal(before: str, after: str, *, is_at_start: bool, is_at_end: bool, is_after_blank_line: bool = False) -> str:
	"""
	The whole lines that are left before and after removed lines, joined. One blank line next to them goes when it would otherwise be left next to another one, directly inside a bracket, or at the start or end of the container: the one before, when a blank line or the end of the container follows, and otherwise the one after, when the removed lines were the first content of the container, as in soml-js. The rest of a run of blank lines stays, so at most one goes. `is_after_blank_line` is for an empty `before` that a blank line outside the container comes before, as in the header of a brace-less document.
	"""
	return ''.join(_removal_parts(before, after, is_at_start=is_at_start, is_at_end=is_at_end, is_after_blank_line=is_after_blank_line))


def _removal_parts(before: str, after: str, *, is_at_start: bool, is_at_end: bool, is_after_blank_line: bool = False) -> tuple[str, str]:
	"""The two sides of a removal with the neighboring blank line adjusted, without copying retained comments together."""
	line_end = after.find('\n')
	is_blank_after = line_end != -1 and _is_blank_line(after[:line_end])
	is_blank_before = _ends_with_blank_line(before) if before else is_after_blank_line

	# `after` without its first line, when it is blank, starts directly after `line_end`.
	if is_blank_before and (is_blank_after or (is_at_end and not after.strip(' \t\n'))):
		if before:
			# The blank line directly before the removed lines, which is the last line of `before`.
			return before[: before.rfind('\n', 0, len(before) - 1) + 1], after

		if is_blank_after:
			return '', after[line_end + 1 :]

	if not before and is_at_start and is_blank_after:
		return '', after[line_end + 1 :]

	return before, after


def _comment_lines_above(node: object, indent: str) -> str:
	"""The comment lines of a member that a program made, each at `indent` and ending with a line break."""
	if not isinstance(node, MemberNode):
		return ''

	# A `str` is a sequence too, so it would be written as one comment line for each character.
	if isinstance(node.comments, str):  # type: ignore[unreachable] # A runtime check, for callers that are not type-checked.
		raise TypeError('The comments of a member are a list of str, one for each line, not a str')

	lines: list[str] = []

	for comment in node.comments:
		if not isinstance(comment, str):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'A comment of a member is a str, not {type(comment).__name__!r}')

		text = exact_str(comment)

		# The final parse does not catch a line break, because the text after it could make a valid member.
		if '\n' in text:
			raise ValueError('A comment of a member cannot hold a line break. Use one item of `comments` for each line')

		lines.append(f'{indent}# {text}\n' if text else f'{indent}#\n')

	return ''.join(lines)


def _indentation_of_line(source: str, offset: int) -> str:
	"""The spaces and tabs at the start of the line that holds `offset`."""
	start = source.rfind('\n', 0, offset) + 1
	end = skip_spaces(source, start)

	return source[start:end]


def _taken_comma(trailing: str, removed_leading: str) -> str:
	"""
	The text after the new last child when the removed children after it take its comma, which they do only when spaces and tabs alone are between the comma and the comments that the first of them owns, which are the block comments on its line before it, as `/* note */` in `[1, /* note */ 2]`.
	"""
	gap = without_comments(trailing)
	index = gap.find(',')
	return trailing[:index] if _is_blank_line(gap[index + 1 :] + without_comments(removed_leading)) else trailing


def _comments_on_line_error(member: MemberNode) -> ValueError:
	return ValueError(
		f'Cannot write the comments of the new member {_format_path((member.key.value,))}, because it goes on the line of the text before it. Comments need a member that starts its own line'
	)


def _ensure_line_end(text: str) -> str:
	return text if text.endswith('\n') else f'{text}\n'


class _Printer:
	"""
	Prints nodes. `changed_text()` returns `None` for a parsed node that did not change, so that its source text is used, and the text otherwise. `depth` is the nesting level, as the parser counts it. `is_one_line` is whether the node is inside a container that stays on one line, as the "Editing" rules of the spec say, so that a container it prints fresh is on one line too, as in `[1, {a: 2}]`.
	"""

	__slots__ = ('changed',)

	def __init__(self) -> None:
		# Remember changed descendants only for this print, so a deep edit checks each subtree at most twice.
		self.changed: set[int] = set()

	def document_text(self, document: DocumentNode) -> str:
		if not isinstance(document.body, _CONTAINER_NODE_TYPES):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
			raise TypeError(f'Expected an ObjectNode or ArrayNode, not {type(document.body).__name__!r}')

		origin = document._origin

		if origin is None:
			return _ensure_line_end(self.text(document.body, '', 1, is_top=True))

		prefix, suffix = origin.trivia
		body = self.changed_text(document.body, '', 1, is_top=True)

		if body is None and document.body is origin.children[0]:
			return origin.source

		body_node = document.body
		body_origin = body_node._origin

		# The line of the first member of a brace-less body begins in the text before the body, so the indentation of that line is there. When another member takes the first line, that indentation goes with the member that left the line: a member from the source that begins its own line keeps its own, and a member the program made has an indentation of its own.
		if body is not None and isinstance(body_node, ObjectNode) and not body_node.braced and body_origin is not None and body_node.members:
			remaining = body_node.members[0]

			if remaining is not body_origin.children[0] and (remaining._origin is None or (remaining.range is not None and _line_indentation(origin.source, remaining.range[0]) is not None)):
				prefix = prefix[: prefix.rfind('\n') + 1]

		text = f'{prefix}{self.source_text(document.body) if body is None else body}{suffix}'
		# A brace-less body owns the text after it, so when it gets braces, it is printed fresh and ends with a line break, as a new body does.
		# The first field of an object's snapshot is `braced` as parsed.
		gets_braces = isinstance(document.body, ObjectNode) and document.body.braced and document.body._origin is not None and not document.body._origin.snapshot[0]
		return text if document.body is origin.children[0] and not gets_braces else _ensure_line_end(text)

	def text(self, node: object, indent: str, depth: int, *, is_top: bool = False, is_one_line: bool = False) -> str:
		changed = self.changed_text(node, indent, depth, is_top=is_top, is_one_line=is_one_line)
		return self.source_text(node) if changed is None else changed

	@staticmethod
	def source_text(node: object) -> str:
		"""The source text of a node that did not change."""
		assert isinstance(node, _Node)  # noqa: S101 # Only a parsed node can be unchanged.
		origin = node._origin
		assert origin is not None  # noqa: S101 # Only a parsed node can be unchanged.
		start, end = origin.range
		return origin.source[start:end]

	def changed_text(self, node: object, indent: str, depth: int, *, is_top: bool = False, is_one_line: bool = False) -> str | None:
		"""`node` is typed as `object`, because a program can put anything into a tree."""
		if not isinstance(node, _Node):
			raise TypeError(f'Expected a node, not {type(node).__name__!r}. Use `from_value()` to make a node from a value')

		if depth > MAX_DEPTH and isinstance(node, _CONTAINER_NODE_TYPES):
			raise ValueError(f'The tree is nested more than {MAX_DEPTH} levels deep, or it is circular')

		if isinstance(node, MemberNode):
			if not isinstance(node.key, KeyNode):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
				raise TypeError(f'Expected a KeyNode, not {type(node.key).__name__!r}')
			_value_node(node.value)

		origin = node._origin

		if origin is None:
			return self.fresh_text(node, indent, depth, is_top=is_top, is_one_line=is_one_line)

		match node:
			# A brace-less object that is no longer the body, such as a whole document moved under a key, is printed fresh, with braces.
			case ObjectNode() if type(node.braced) is bool and node.braced == origin.snapshot[0] and (node.braced or is_top):  # type: ignore[redundant-expr] # A runtime check, for callers that are not type-checked.
				return self.container_text(node, node.members, indent, depth, is_top=is_top, is_inside_one_line=is_one_line)
			case ObjectNode():
				return self.fresh_text(node, indent, depth, is_top=is_top, is_one_line=is_one_line)
			case ArrayNode():
				return self.container_text(node, node.elements, indent, depth, is_top=is_top, is_inside_one_line=is_one_line)
			case MemberNode():
				if node.comments:
					raise ValueError(
						f'Cannot write the comments of the member {_format_path((node.key.value,))}, because it is from the source. Its comments are in the source, and `comments` is only for a member that a program made'
					)

				# The text between the key and the value, which holds the `:` and any comment there.
				between = origin.trivia[0]
				value_origin = origin.children[1]._origin
				assert value_origin is not None  # noqa: S101 # Every child from the source has an origin.
				value_start = value_origin.range[0]
				# A block string begins on the line after its key, and a changed value is never a block string, so it goes on the key's line, as `format()` writes it. A comment between them keeps it on its own line. The source tells whether the value was a block string, because a program can change the node.
				moves_up = origin.source.startswith(("'''", '"""'), value_start) and not between[1:].strip(' \t\n')
				# A value that stays on a line of its own, after a comment, is indented from that line, not from its key.
				value_indent = indent if moves_up else _indentation_at(origin.source, value_start, indent)
				key = self.changed_text(node.key, indent, depth)
				value = self.changed_text(node.value, value_indent, depth, is_one_line=is_one_line)

				if key is None and value is None and node.key is origin.children[0] and node.value is origin.children[1]:
					return None

				if value is not None and moves_up:
					between = ': '

				return f'{self.source_text(node.key) if key is None else key}{between}{self.source_text(node.value) if value is None else value}'
			case _ if node._snapshot() == origin.snapshot:
				return None
			case _:
				return self.fresh_text(node, indent, depth, is_top=is_top, is_one_line=is_one_line)

	def container_text(self, node: ObjectNode | ArrayNode, children: Sequence[_Node], indent: str, depth: int, *, is_top: bool, is_inside_one_line: bool) -> str | None:
		"""
		The text of a parsed container, keeping the source around the children that remain.

		A child owns the comment lines and indentation before it, with the block comments on its line before it, and after it the comments before its comma, its comma, and the rest of its last line when only comments are there, as the "Editing" rules of the spec say. A removed child takes the comments on its own lines with it, and leaves the comment lines above it. A new child at the index of a removed one takes its place, with its comments and layout, and a new child gets the layout of the children around it, as in the Rust implementation.

		Commas follow the "Editing" rules of the spec, as in soml-js: a new child on its own line has no comma, a removed child takes its comma with it, the removed children at the end take the comma before them when only spaces and tabs are between it and the comments that the first of them owns, and every other comma stays.

		A container stays on one line, as in soml-js, when its brackets are on one line in the source with something other than spaces and tabs between them, or when it is inside a container that stays on one line. Its new children are written on one line, and in a container with no children from the source, they go before the closing bracket, so `[/* note */]` becomes `[/* note */ 1]`.
		"""
		origin = node._origin
		assert origin is not None  # noqa: S101 # Only called for a parsed node.

		if node._is_unchanged(self.changed):
			return None

		_check_children(node)
		original = origin.children
		positions = {id(child): position for position, child in enumerate(original)}
		opening = origin.trivia[0]
		closing = origin.trivia[-1]
		leadings = origin.trivia[1:-1:2]
		trailings = origin.trivia[2:-1:2]
		is_bare = isinstance(node, ObjectNode) and not node.braced and is_top
		# Where the content of a brace-less body starts, before the block comments that its first member owns, as `/* note */` in `/* note */ a: 1`.
		content_start = origin.range[0] - len(leadings[0]) if is_bare else origin.range[0]
		container_indent = _indentation_at(origin.source, content_start, indent)
		first_line_indentation = (_line_indentation(origin.source, content_start) or '') if is_bare else ''

		remaining = {id(child) for child in children}
		# The position in the source that each child takes. A node that `set()` put in the place of a removed one takes its position.
		slots = {identity: position for identity in remaining if (position := positions.get(identity)) is not None}
		taken = set(slots.values())

		for child in children:
			replaced = getattr(child, '_replaces', None)
			identity = id(child)
			replaced_position = positions.get(id(replaced))

			if identity not in positions and replaced_position is not None and id(replaced) not in remaining and replaced_position not in taken:
				slots[identity] = replaced_position
				taken.add(replaced_position)

		# A brace-less object needs at least one member, so when every member goes, an empty object with braces takes the place of the first one, with the comments that it owns, as in soml-js.
		if is_bare and not children:
			children = [ObjectNode()]
			slots[id(children[0])] = 0
			taken.add(0)

		# A container that is empty, or whose children from the source were all removed, is written on several lines when it gets new children, as canonical form writes it, unless it stays on one line.
		is_emptied = not taken
		# Whether it stays on one line: its brackets are on one line, and something other than spaces and tabs is between them, without the children from the source when they were all removed, so that it is as two separate edits see it. An empty `[]` or `{}` says nothing about layout, because the formatter writes every empty container that way.
		container_start, container_end = origin.range
		is_on_one_line_in_source = not is_bare and origin.source.find('\n', container_start, container_end) == -1
		is_one_line = (
			not is_bare and is_on_one_line_in_source and (is_inside_one_line or bool((opening + closing if is_emptied else origin.source[container_start + 1 : container_end - 1]).strip(' \t')))
		)
		is_multiline = is_bare or (is_emptied and not is_one_line) or any('\n' in text for text in origin.trivia)
		# A new child with no child before it gets the indentation of the last child from the source that starts its own line, or one level more than the container.
		line_indentations = (_line_indentation(child._origin.source, child._origin.range[0]) for child in reversed(original) if id(child) in remaining and child._origin is not None)
		first_child_indent = next((indentation for indentation in line_indentations if indentation is not None), container_indent + ('' if is_bare else '\t')) if is_multiline else indent
		# The indentation of the line of each child from the source, also when it does not start that line, as the second item in `[1,\n\t2, 3]`, so that a new value in its place lines up with that line. A line break is looked for only between a child and the one before it, so that a long line takes linear time.
		# A line that a block comment from an earlier line runs into, as in `/* a\nb */ c: 1`, is indented as the line where that comment starts, as in soml-js.
		line_indentation = container_indent
		child_indentations: list[str] = []
		previous_start = previous_end = origin.range[0]

		for child in original:
			# A child from the source has a range.
			start, end = child.range or (0, 0)
			line_break = without_comments(origin.source[previous_end:start]).rfind('\n')

			if line_break != -1:
				line_indentation = _indentation_of_line(origin.source, previous_end + line_break + 1)
			elif origin.source.find('\n', previous_start, previous_end) != -1:
				# The child before it ends on a later line than it starts, and this child is on that line.
				line_indentation = _indentation_of_line(origin.source, previous_end)

			child_indentations.append(line_indentation)
			previous_start, previous_end = start, end

		# The indentation of each child. A new child on its own line gets the indentation of the line of the child before it, as in soml-js, and one on the line where the child before it ends gets the indentation of that line.
		indentations: list[str] = []

		for index, child in enumerate(children):
			previous_slot = slots.get(id(children[index - 1])) if index else None
			slot = slots.get(id(child))

			if slot is not None:
				indentations.append(child_indentations[slot])
			elif not is_bare and previous_slot is not None and not trailings[previous_slot].endswith('\n'):
				# A child from the source has a range.
				previous_start, previous_end = original[previous_slot].range or (0, 0)
				indentations.append(_indentation_of_line(origin.source, previous_end) if origin.source.find('\n', previous_start, previous_end) != -1 else child_indentations[previous_slot])
			else:
				indentations.append(indentations[-1] if is_multiline and indentations else first_child_indent)

		texts = [self.changed_text(child, indentation, depth + 1, is_one_line=is_one_line) for child, indentation in zip(children, indentations, strict=True)]

		# A removed child that nothing replaces leaves the comment lines above it, which move to the next child that remains, or to the end. The blank lines between removed children that nothing else separates go with them.
		comments_before: dict[int, str] = {}
		is_after_removal: set[int] = set()
		pending_parts: list[str] = []
		is_removing = False
		# Whether the container's content starts at its first child, which a brace-less body does only when nothing but blank lines is before it.
		is_content_start = not is_bare or not origin.source[:content_start].strip(' \t\n')
		is_at_start = is_content_start
		# Whether the header of a brace-less body ends with a blank line, which counts as a blank line before its first child.
		header = origin.source[:content_start].rstrip(' \t') if is_bare and not is_content_start else ''
		is_after_blank_line = _ends_with_blank_line(header)

		for position in range(len(original)):
			if position in taken:
				comments_before[position] = ''.join(pending_parts)

				if is_removing:
					is_after_removal.add(position)

				pending_parts.clear()
				is_removing = False
				is_at_start = False
			else:
				lines = _comment_lines(leadings[position])

				if not is_removing:
					pending_parts = [lines] if lines else []
				elif lines.strip(' \t\n'):
					# The blank line at the end of the header of a brace-less body is before these lines only when no child before them is kept.
					before = pending_parts.pop() if pending_parts else ''
					before, lines = _removal_parts(before, lines, is_at_start=is_at_start, is_at_end=False, is_after_blank_line=is_after_blank_line and not comments_before)
					if before:
						pending_parts.append(before)
					pending_parts.append(lines)

				is_removing = True

		pending = ''.join(pending_parts)

		# Whether the last child in the source has a comma, and whether the document ended with a line break, which the children keep when they change.
		last_has_comma = bool(original) and origin.commas[-1]
		has_final_line_break = trailings[-1].endswith('\n') if original else True
		# The last position in the source that a child takes.
		last_slot = max(taken, default=-1)

		if not children:
			rest = opening + pending + closing

			# Only whitespace is left, so the container is written as canonical form writes an empty one.
			if not rest.strip(' \t\n'):
				return '{}' if isinstance(node, ObjectNode) else '[]'

			# The children on the line of the opening bracket took the line break at its end with them, and the comment lines after them still begin a line of their own, as in soml-js. In a container on one line, the comments that are left stay on it, as in `[/* note */]`.
			if not opening.endswith('\n') and not is_on_one_line_in_source:
				opening = opening.rstrip(' \t') + '\n'

		if is_emptied and children and is_one_line:
			# The new children go before the closing bracket, after what is left between the brackets, such as a comment.
			opening = (opening + closing).rstrip(' \t')
			opening += ' ' if opening else ''
			closing = ''
		elif is_emptied and children and not is_bare:
			if not original and '\n' in opening + closing:
				# The comment lines in the empty container, such as `# Add servers here.`, stay above the new children.
				comment_lines = _comment_lines(closing)
				opening += comment_lines
				closing = closing[len(comment_lines) :]

				# A block comment that spans lines does not end the line, and the new children go on lines of their own.
				if not opening.endswith('\n'):
					opening = opening.rstrip(' \t') + '\n'
					closing = closing or container_indent
			elif not opening.endswith('\n'):
				# The new children go on lines of their own. The text before the closing bracket stays on its own line when it began one in the source, without the blank lines at its start, and otherwise a comment in it stays on the line of the opening bracket. The line break before it can belong to a removed child, so the source tells.
				if _line_indentation(origin.source, origin.range[1] - 1 - len(closing)) is not None:
					opening = opening.rstrip(' \t') + '\n'
					closing = closing[_blank_run_end(closing) :]
				else:
					opening = (opening + closing).rstrip(' \t') + '\n'
					closing = container_indent

		# Whether the closing bracket begins its line in the source.
		closing_starts_line = not is_bare and _line_indentation(origin.source, origin.range[1] - 1) is not None
		# When the first child from the source was removed and the one that takes the first line begins its own line, the line of the removed child keeps only the opening bracket, so the indentation it held in the text before the child goes with it. A comment on a line of its own there stays.
		first_slot = slots.get(id(children[0])) if children else None
		first_takes_own_line = first_slot is not None and first_slot > 0 and '\n' in without_comments(trailings[0])

		if first_takes_own_line and not is_bare:
			opening = opening[: without_comments(opening).rfind('\n') + 1]

			if not opening.endswith('\n'):
				opening += '\n'

		parts = ['' if is_bare else origin.source[origin.range[0]], opening]
		# Whether the line ends before the next child, after the opening bracket or after the child before it. A new child goes on its own line when it does, and otherwise on that line after a comma, as in soml-js.
		ends_line = is_bare or opening.endswith('\n')
		# Whether the child before has a comma that a new child on its line copies.
		previous_has_comma = False
		# The comments after the child before a new child on its line, which go after the new child.
		carried = ''

		for index, (child, text) in enumerate(zip(children, texts, strict=True)):
			slot = slots.get(id(child))
			body = self.source_text(child) if text is None else text
			is_last = index == len(children) - 1

			if slot is None:
				# A comment line would split the line of the text before the new child.
				if not ends_line and isinstance(child, MemberNode) and child.comments:
					raise _comments_on_line_error(child)

				leading = f'{_comment_lines_above(child, indentations[index])}{indentations[index]}' if ends_line else ''
				# A line break separates a new child on its own line, so it needs no comma. On the line of the child before it, a comma separates them, and the new child has a comma after it when the child before has one.
				has_comma = not ends_line and previous_has_comma
				# A new last member of a brace-less object ends the document without a line break when the source did, unless text, such as a comment line, follows it. A new last child on the line of the child before it ends that line when the closing bracket began a line of its own in the source, as the line of a removed child would.
				ends_own_line = ends_line and (not is_bare or not is_last or has_final_line_break or pending or closing)
				trailing = (',' if has_comma else '') + carried + ('\n' if ends_own_line or (is_last and closing_starts_line) else '')
			else:
				leading = leadings[slot]
				trailing = trailings[slot]
				has_comma = origin.commas[slot]

				# The first child is removed, so this one takes its place on its line, with its indentation, and with the comments that are on its own line before it. On the line of the removed child, it takes that line's indentation. On a line of its own, it keeps its own indentation, and the line of the removed child keeps only what comes before it, so the indentation that the removed child held goes with it.
				if index == 0 and slot > 0:
					own_line = leadings[slot][len(_comment_lines(leadings[slot])) :]
					first_own_line = leadings[0][len(_comment_lines(leadings[0])) :]
					first_indentation = first_own_line[: len(first_own_line) - len(first_own_line.lstrip(' \t'))]
					leading = leadings[slot] if first_takes_own_line else _comment_lines(leadings[slot]) + first_indentation + own_line.lstrip(' \t')
				elif index > 0 and ends_line and '\n' not in leading and _line_indentation(origin.source, original[slot].range[0]) is None:  # type: ignore[index] # A child from the source has a range.
					# A line ends before it, but it did not start its line in the source, as after a removed child on its line, so it gets the indentation of its line. The first child in its place keeps the text before it, such as a comment in the header of a brace-less body.
					leading = _indentation_of_line(origin.source, original[slot].range[0]) + leading.lstrip(' \t')  # type: ignore[index]

				if slot in is_after_removal:
					leading = _join_at_removal(comments_before[slot], leading, is_at_start=index == 0 and is_content_start, is_at_end=False, is_after_blank_line=index == 0 and is_after_blank_line)
				else:
					leading = comments_before[slot] + leading

			# The text before the body keeps the indentation only of the line of the first member from the source, so a child from the source that is no longer first needs it, and when another child takes the first line, the text before the body gives the old indentation up, which the printer of the document cuts.
			if is_bare and index > 0 and slot == 0:
				leading = first_line_indentation + leading

			is_followed_as_in_source = slot is not None and index + 1 < len(children) and slots.get(id(children[index + 1])) == slot + 1

			if slot is None:
				carried = ''

			# The children after it in the source were removed. They take the comma before them when the last of them has none, and only spaces and tabs are between that comma and them.
			is_last_from_source = slot is not None and slot < len(original) - 1 and slot == last_slot
			without_taken_comma = _taken_comma(trailing, leadings[slot + 1]) if is_last_from_source and has_comma and not last_has_comma else trailing  # type: ignore[operator] # `is_last_from_source` means `slot` is not `None`.
			# A new child after it keeps that comma to separate them, but does not copy it, so `[1, 2]` without `2` and with a new `3` is `[1, 3]`, as two separate edits give.
			copies_comma = has_comma and without_taken_comma == trailing

			if is_last:
				if is_last_from_source:
					trailing = without_taken_comma

					# The spaces before the bracket go, and the line break after them stays. The spaces before a comment that is left there stay, as in `[1, /* note */]`.
					if not trailing.endswith('\n') and not closing.strip(' \t\n'):
						trailing = trailing.rstrip(' \t') + ('\n' if trailings[-1].endswith('\n') else '')
			elif is_followed_as_in_source:
				# The text between two children that were next to each other in the source stays as it is.
				pass
			elif is_bare or (is_multiline and (trailing.endswith('\n') or (slot is not None and id(children[index + 1]) in slots))):
				# The line break separates it from the child after it. The spaces and tabs at its end are the line's own and stay, as soml-js keeps them.
				if not trailing.endswith('\n'):
					trailing = f'{trailing if is_bare else trailing.rstrip(" \t")}\n'
			else:
				# The child after it stays on this line, as a new child does after a child that something follows on its line, so a comma separates them.
				if id(children[index + 1]) not in slots:
					# A new child goes directly after the comma, or after the comments that the child owns when it has none, and the text after that moves to after the new child, as in soml-js.
					comma_end = without_comments(trailing).find(',') + 1 if has_comma else len(trailing.rstrip(' \t'))
					carried = trailing[comma_end:]
					trailing = trailing[:comma_end]

					# The spaces at the end separated it from a child after it that was removed, not from the closing bracket.
					if slot is None or slot < len(original) - 1:
						carried = carried.rstrip(' \t')

				if not has_comma:
					# The comma goes after the comments that the child owns, so that they stay with it, as `[1, 2 /* note */, 3]`.
					trailing = f'{trailing.rstrip(" \t")},'

				# A space after the comma, unless it is the comma of the source between two children from the source.
				if not trailing.endswith((' ', '\t', '\n')) and not (slot is not None and origin.commas[slot] and id(children[index + 1]) in slots):
					trailing += ' '

			parts.extend((leading, body, trailing))
			ends_line = trailing.endswith('\n')
			previous_has_comma = copies_comma

		ending = _join_at_removal(pending, closing, is_at_start=not children and is_content_start, is_at_end=True) if is_removing else pending + closing
		parts.extend((ending, '' if is_bare else origin.source[origin.range[1] - 1]))
		return ''.join(parts)

	def fresh_text(self, node: _Node, indent: str, depth: int, *, is_top: bool = False, is_one_line: bool = False) -> str:
		"""The text of a node that a program made or changed, in canonical layout at the given indentation, but in the order and with the styles it has. With `is_one_line`, a container is written on one line instead, as in `[1, {a: 2}]`."""
		match node:
			case ObjectNode(members=members):
				_check_children(node)
				inner = f'{indent}\t'
				# Checked, because anything is true or false in Python, so a wrong value would print without an error.
				if type(node.braced) is not bool:  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
					raise TypeError(f'An ObjectNode holds a bool for braced, not {type(node.braced).__name__!r}')

				if is_top and not node.braced and members:
					return '\n'.join(f'{_comment_lines_above(member, "")}{self.text(member, "", depth + 1)}' for member in members)

				if not members:
					return '{}'

				if is_one_line:
					# A comment line would split the line.
					for member in members:
						if _comment_lines_above(member, inner):
							raise _comments_on_line_error(member)

					return f'{{{", ".join(self.text(member, inner, depth + 1, is_one_line=True) for member in members)}}}'

				lines = ''.join(f'{_comment_lines_above(member, inner)}{inner}{self.text(member, inner, depth + 1)}\n' for member in members)
				return f'{{\n{lines}{indent}}}'
			case ArrayNode(elements=elements):
				_check_children(node)
				inner = f'{indent}\t'
				if not elements:
					return '[]'

				if is_one_line:
					return f'[{", ".join(self.text(element, inner, depth + 1, is_one_line=True) for element in elements)}]'

				lines = ''.join(f'{inner}{self.text(element, inner, depth + 1)}\n' for element in elements)
				return f'[\n{lines}{indent}]'
			case MemberNode():
				return f'{self.text(node.key, indent, depth)}: {self.text(node.value, indent, depth, is_one_line=is_one_line)}'
			case KeyNode(style=style):
				if style not in ('bare', 'literal', 'escaped'):
					raise ValueError(f"The style of a KeyNode is 'bare', 'literal', or 'escaped', not {style!r}")

				value = node._checked_value()

				if style == 'bare':
					return format_key(value)

				return format_escaped_string(value, 'key') if style == 'escaped' else format_string(value, 'key')
			case StringNode(style=style):
				if style not in ('literal', 'escaped'):
					raise ValueError(f"The style of a StringNode is 'literal' or 'escaped', not {style!r}")

				value = node._checked_value()
				return format_escaped_string(value, 'string') if style == 'escaped' else format_string(value, 'string')
			case IntegerNode(radix=radix):
				integer = node.to_value()

				# A float such as 10.0 compares equal to its int, so the type is checked too. Checked first, because a negative value is written in decimal whatever its radix.
				radix_object: object = radix

				if isinstance(radix_object, bool) or not isinstance(radix_object, int) or radix_object not in (2, 8, 10, 16):  # pyright: ignore[reportUnnecessaryIsInstance] # A runtime check, for callers that are not type-checked.
					raise ValueError(f'The radix of an IntegerNode is 2, 8, 10, or 16, not {radix!r}')

				if integer < 0 or radix == 10:
					return int.__repr__(integer)

				prefix, digits_format = RADIX_FORMATS[radix]
				return f'{prefix}{integer:{digits_format}}'
			case FloatNode():
				# An int is allowed, because a type checker accepts an int for a float. A bool is not.
				return format_float(node.to_value())
			case BooleanNode():
				# Checked, because anything is true or false in Python, so a wrong value would print without an error.
				return 'true' if node.to_value() else 'false'
			case NullNode():
				return 'null'
			case InstantNode():
				return format_instant(node._checked_value())
			case DateNode():
				return date.isoformat(node.to_value())
			case DurationNode():
				return format_duration(node._checked_value())
			case _:
				raise TypeError(f'Expected a node, not {type(node).__name__!r}')
