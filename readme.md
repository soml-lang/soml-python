# soml

> Read and write [SOML](https://soml.sh), a config format for humans, in Python

> [!NOTE]
> The format is a draft.

## Highlights

- Strict: every rule in the specification is enforced, and every error says what is wrong and where
- Exact: an int is an `int` and a float is a `float`, so `3` and `3.0` keep their types
- Canonical: `dumps(value, canonical=True)` writes the one canonical form, so equal values give equal text, and by default `dumps()` keeps the order of your keys
- Formatter: `format()` normalizes the layout of a file and keeps its comments and spelling, with the same output as the JavaScript implementation
- Lossless editing: `soml.tree` changes a value in a file and keeps its comments, order, and spelling
- Familiar: the API of `json` and `tomllib`
- Fast for pure Python: parses the shared fixtures about 1.5 to 1.8 times as fast as `tomllib`, which is pure Python too (see [Benchmark](#benchmark))
- Typed: strict type hints, checked with mypy and pyright in strict mode
- No dependencies

## Install

```sh
pip install soml-lang
```

Requires Python 3.14 or later.

## Usage

```python
>>> import soml
>>> config = soml.loads('''
... # The edge service.
... name: 'api-gateway'
... replicas: 3
... timeout: 30.0
... grace: 1m30s
... launch-date: 2026-09-19
... deployed-at: 2026-09-19T14:00:00Z
... postgres: {host: 'db.internal'}
... ''')
>>> config['replicas'], config['timeout']
(3, 30.0)
>>> config['grace']
datetime.timedelta(seconds=90)
>>> config['postgres']
{'host': 'db.internal'}
>>> print(soml.dumps(config), end='')
name: 'api-gateway'
replicas: 3
timeout: 30.0
grace: 1m30s
launch-date: 2026-09-19
deployed-at: 2026-09-19T14:00:00Z
postgres: {
	host: 'db.internal'
}

```

Read and write files in binary mode, as with `tomllib`:

```python
with open('config.soml', 'rb') as file:
	config = soml.load(file)

with open('config.soml', 'wb') as file:
	soml.dump(config, file)
```

## Types

| SOML | Python | `dumps()` also accepts |
|---|---|---|
| string | `str` | `str` subclasses, such as `StrEnum` |
| int | `int` | `int` subclasses, such as `IntEnum` |
| float | `float` | `float` subclasses |
| bool | `bool` | |
| null | `None` | |
| date | `datetime.date` | `date` subclasses, except `datetime` and its subclasses |
| instant | `datetime` in UTC | a `datetime` with any time zone |
| duration | `timedelta` | |
| array | `list` | `tuple` and any other `Sequence`, except `str`, `UserString`, `bytes`, `bytearray`, and `memoryview` |
| object | `dict`, in document order | any `Mapping` with `str` keys |

An int is an `int` and a float is a `float` in both directions, so `3` and `3.0` stay different:

```python
>>> soml.loads('a: 3\nb: 3.0')
{'a': 3, 'b': 3.0}
>>> soml.dumps({'a': 3, 'b': 3.0})
'a: 3\nb: 3.0\n'

```

> [!IMPORTANT]
> Python considers `1 == 1.0 == True` equal, so `soml.loads('a: 1') == soml.loads('a: 1.0') == soml.loads('a: true')` is `True` even though the types differ. Compare the types too when that matters.

With this mapping, two things always hold:

- `loads(dumps(value)) == value`, for a value made of the types `loads()` returns. (A `datetime` comes back in UTC, a `tuple` as a `list`, and `-0.0` as `0.0`.)
- `dumps(loads(text), canonical=True)` is the canonical form of `text`, and `dumps(loads(canonical), canonical=True) == canonical`.

### Dates, instants, and durations

A date is a `datetime.date`, distinct from `datetime.datetime`: it has no time or timezone. Dates use strict Gregorian validation for years 0001 through 9999, including historical dates such as `1582-10-10`. They never pass through an instant.

```python
>>> soml.loads('launch-date: 2026-09-19')['launch-date']
datetime.date(2026, 9, 19)

```

> [!WARNING]
> The specification requires nanosecond precision, but `datetime` and `timedelta` stop at microseconds. A document with an instant or a duration that has a nonzero part below a microsecond, such as `1ns` or `2026-09-19T14:00:00.123456789Z`, is rejected with a `SOMLDecodeError`. It is never rounded. A fraction with trailing zeros, such as `.123000000`, is fine. `format()` accepts it, because it never converts a value.

An instant is read as a `datetime` in UTC, because the offset is how the instant was written, not part of its value. `dumps()` needs a `datetime` with a time zone, and writes it in UTC:

```python
>>> soml.loads('a: 2026-09-19T14:00:00+02:00')['a']
datetime.datetime(2026, 9, 19, 12, 0, tzinfo=datetime.timezone.utc)

```

## API

### loads(text)

Parse a document. Returns a `dict` or a `list`, because a document is always a collection.

`text` is a `str`, or UTF-8 `bytes` or `bytearray`. Bytes are decoded strictly, and invalid UTF-8 is reported with its position.

Raises `SOMLDecodeError` when the document is not valid, and `TypeError` when `text` is not a `str` or bytes.

### load(file)

Parse a document from a file opened in binary mode, like `open('config.soml', 'rb')`.

Raises `SOMLDecodeError` when the document is not valid, and `TypeError` when the file is opened in text mode.

### `dumps(value, *, canonical=False)`

Serialize a `dict` or a `list`. Members keep the order of the input, nesting is written with braces and tabs, every member and item is on its own line with no commas, and the text ends with one line feed. Comments and block strings are never written.

The output follows every rule of canonical form except member order: members keep the order of the input, because a person reads the file, and sorted keys put `description` before `name`:

```python
>>> print(soml.dumps({'name': 'api', 'description': 'The edge service.'}), end='')
name: 'api'
description: 'The edge service.'

```

With `canonical=True`, members are sorted by key, and the output is [canonical form](https://github.com/soml-lang/soml/blob/main/spec.md#canonical-form): equal values give equal text, so it can be hashed, signed, or compared.

```python
>>> print(soml.dumps({'name': 'api', 'description': 'The edge service.'}, canonical=True), end='')
description: 'The edge service.'
name: 'api'

```

Raises `TypeError` for a value of a type that cannot be written, such as a `set`, a non-`str` key, or a scalar at the top level, and when `canonical` is not a `bool`. Raises `ValueError` for a value of the right type that cannot be written: NaN, an int outside the 64-bit range, a string or key with a carriage return or a lone surrogate, a `datetime` without a time zone, a `datetime` outside the years 0001 to 9999 in UTC, a `timedelta` outside the 64-bit range of nanoseconds, a circular reference, or nesting deeper than 100 levels.

### dump(value, file, *, canonical=False)

Write a `dict` or a `list` to a file opened in binary mode, like `open('config.soml', 'wb')`, as `dumps()` writes it. Raises the same errors as `dumps()`, and writes nothing when it does. Raises `TypeError` when the file is opened in text mode. The stream must complete the write, as buffered binary files do; an incomplete write raises `OSError`.

### `format(text)`

Format a document. Returns it with its layout normalized, ending with one line feed. `text` is a `str`, or UTF-8 `bytes` or `bytearray`.

The layout follows the formatter in the specification: one tab per level, every member and item on its own line with no commas, and no trailing whitespace or runs of blank lines. An object or an array whose brackets are on one line stays on one line, as in `ports: [80, 443]`, with a comma and a space between its members or items. To give a one-line container one member or item per line, put a line break anywhere inside it. Comments, member order, block strings, and the spelling of every value stay as they are, so the value never changes. A file is already formatted when `format()` returns it unchanged.

```python
>>> print(soml.format('pool: {min: 2,   max: 16,} # Connections'), end='')
pool: {min: 2, max: 16} # Connections
>>> print(soml.format('pool: {\nmin: 2, max: 16}'), end='')
pool: {
	min: 2
	max: 16
}

```

In detail, as the specification states:

- A block comment that a value follows on the same line, as in `1, /* note */ 2`, stays in front of that value when each item goes on its own line.
- One space follows each `:`, as in canonical form.
- A value on the line after its `key:` moves up to that line, unless a comment comes between them. Then it stays on its own line, one level deeper than the key.
- A block string begins on the line after its key, and its delimiters and content have the indentation of that line. Its lines are not otherwise changed.
- There are no blank lines at the start of the file or directly inside brackets.
- The one change inside a block comment is the same layout rule: trailing whitespace is removed, and runs of blank lines collapse to one.

Raises the same `SOMLDecodeError` as `loads()`, and `TypeError` when `text` is not a `str` or bytes. The exception is an instant or a duration finer than a microsecond: `format()` accepts it, because it keeps the text of every value and never converts it to a `datetime` or a `timedelta`.

### `SOMLDecodeError`

Raised by `loads()`, `load()`, `format()`, and `soml.tree.parse()`. A subclass of `ValueError`, with the same attributes as `json.JSONDecodeError` and `tomllib.TOMLDecodeError`:

```python
>>> try:
... 	soml.loads('name: api-gateway')
... except soml.SOMLDecodeError as error:
... 	print(error)
... 	print(error.lineno, error.colno)
Unexpected “api-gateway”. A string value must be quoted, as in 'api-gateway' (at line 1, column 7)
1 7

```

| Attribute | |
|---|---|
| `msg` | What is wrong, without the position |
| `doc` | The document. For bytes that are not valid UTF-8, the part before the first invalid byte. |
| `pos` | The index in `doc` |
| `lineno` | The 1-based line |
| `colno` | The 1-based column, counted in code points |
| `code_frame` | Up to three lines that end at the error, with a caret under it, as the JavaScript and Rust implementations draw it |

```python
>>> try:
... 	soml.loads('a: 1\nb: nope')
... except soml.SOMLDecodeError as error:
... 	print(error.code_frame)
  1 | a: 1
> 2 | b: nope
    |    ^

```

The frame is also the error's note (`error.__notes__`, [PEP 678](https://peps.python.org/pep-0678/)), so an uncaught error shows it in the traceback, under the message. `str(error)` is only the message.

A long line is clipped around the error, and characters that a terminal acts on, such as an escape or a bidirectional control, are replaced in the frame and the message.

### Types for type checkers

- `soml.Value`: a value that `loads()` returns.
- `soml.Document`: what `loads()` returns, a `dict[str, Value]` or a `list[Value]`.
- `soml.Serializable`: a value that `dumps()` accepts.

The examples here leave out the narrowing that a type checker needs. `loads()` returns a `dict` or a `list`, and a value can be any of the types above, so typed code checks first:

```python
config = soml.loads(text)
assert isinstance(config, dict)
port = config['port']
assert isinstance(port, int)
```

The same applies to the tree: `document.body` is an `ObjectNode` or an `ArrayNode`, `get()` returns `None` when no node is at the path, and a member's `value` is any value node. The nodes are dataclasses, so `match` narrows them and reads their fields in one step:

```python
>>> from soml import tree
>>> document = tree.parse('port: 0x1F90')
>>> match document.get('port'):
... 	case tree.IntegerNode(value=port, radix=16):
... 		print(f'{port} in hexadecimal')
... 	case tree.IntegerNode(value=port):
... 		print(port)
... 	case None:
... 		print('No port')
... 	case _:
... 		print('The port is not an int')
8080 in hexadecimal

```

## Syntax tree

`soml.tree` reads a document into a syntax tree that keeps everything the author wrote. Change the nodes, then print the tree, and whatever you did not change is copied from the source byte for byte, comments included. Use it to change a config file from a program without rewriting the rest of it.

```python
>>> from soml import tree
>>> document = tree.parse('''\
... # The edge service.
... name: 'api-gateway'
... port: 0x1F90  # Hex, on purpose.
... postgres: {host: 'db.internal'}
... tags: ['edge', 'eu']
... ''')
>>> document.get('port').value = 9090
>>> document.set(('postgres', 'host'), 'db2.internal')
>>> document.set(('tags', 2), 'canary')
>>> document.set('debug', False)
>>> print(tree.unparse(document), end='')
# The edge service.
name: 'api-gateway'
port: 0x2382  # Hex, on purpose.
postgres: {host: 'db2.internal'}
tags: ['edge', 'eu', 'canary']
debug: false

```

### Paths

`DocumentNode` reads and changes values by path. A path is a key, an array index, or a sequence of keys and indices, such as `('servers', 0, 'port')`.

#### `document.get(path)`

The value node at a path, or `None` when no node is there. Set `value` on the node to change a value and keep its spelling, as with `0x1F90` above.

All three methods, `in`, and `del` raise `ValueError` for an empty path or a negative index, and `TypeError` for an item of a path that is not a `str` or an `int`.

#### `document.set(path, value)`

Set a plain value at a path, replacing what is there or adding it:

- A replaced value keeps the comments around it.
- A new member goes after the last member of its object, with that member's layout. It goes on its own line with no comma, because the line break separates it, after the comments that the member before it owns (see `remove()`), so before a comment on a line of its own. When something other than its comma and those comments follows the member before it on that member's line, such as the `}` of `{a: 1}`, it goes on that line after a comma instead, and it has a comma after it when the member before has one. So `{a: 1}` becomes `{a: 1, b: 2}`, and `[1, 2 /* note */]` with a new item becomes `[1, 2 /* note */, 3]`.
- A missing parent object is made with braces, written on several lines, or on one line in a container that stays on one line.
- An index equal to the length of an array appends to it. A path below it makes that item, with objects for the keys after it, so `('b', 0, 'c')` in `b: []` adds the item `{c: 1}`.
- In a container that stays on one line, a new or replaced value is written on one line too, so `[1, 2]` with a new item `{'a': 3}` becomes `[1, 2, {a: 3}]`. A container stays on one line when its brackets are on one line with something other than spaces and tabs between them, as in `[1, 2]` and `[/* note */]`, or when it is inside a container that stays on one line. A new member or item in a container with no members or items goes on a line of its own, as in an empty `[]` or `{}`, unless the container stays on one line: then it goes before the closing bracket, so `[/* note */]` becomes `[/* note */ 1]`, and `a: [1, []]` becomes `a: [1, [2]]`.

Raises `ValueError` when the path does not fit the document (it goes through a scalar, uses a key on an array or an index on an object, has an index past the end, or has an index below a value that does not exist), when the result would not be a valid document, and with the errors of `dumps()` for a value that cannot be written. The document is unchanged after an error. Each change reads the whole document back to check it.

#### `document.remove(path)`

Remove the member or item at a path, with the comments it owns and the comma after it. A member or item owns the comments that `format()` keeps with it, as the "Editing" rules of the specification say: the comments after it on its line, also after its comma when nothing else follows there, and the block comments before it on its line, after the comma or bracket before it. So removing `2` from `[1, /* note */ 2]` gives `[1]`. A comment on a line of its own belongs to no member or item, so the comment lines above it stay. When it is the last one and has no comma, it takes the comma before it instead, when only spaces, tabs, and the comments it owns are between, so `[1, 2]` becomes `[1]`. No other comma changes. Removing the only member of a document without braces leaves `{}` in its place, because a document is never empty. Like a value that `set()` replaces, such an empty object keeps the comments around the member, so `port: 8080 # Note` becomes `{} # Note`.

Comment ownership and layout refer to the original source throughout a sequence of edits. For example, adding and then removing an item in `[/* note */]` restores `[/* note */]`. To apply each edit to the text produced by the previous one, reparse it with `document = tree.parse(tree.unparse(document))` between edits.

Removing a value that does not exist changes nothing and is not an error: a missing member, a missing object or array on the way, or an index at or past the end of its array. So removing the same path twice is safe. Returns whether something was removed, so a caller that expected a value can tell that it was missing. Raises `ValueError` when the path does not fit the document before it leads to nothing: it goes through a scalar, or uses a key on an array or an index on an object. The document is unchanged after an error.

```python
>>> document = tree.parse('port: 8080\n')
>>> document.remove('port'), document.remove('port')
(True, False)

```

#### path in document, del document[path]

`in` tells whether there is a value at a path. `del` removes as `remove()` does, but when nothing is there, where `remove()` returns `False`, `del` raises `KeyError`, as on a `dict`:

```python
>>> document = tree.parse("postgres: {host: 'db.internal'}\nport: 8080\n")
>>> 'postgres' in document, ('postgres', 'port') in document
(True, False)
>>> del document['port']
>>> print(tree.unparse(document), end='')
postgres: {host: 'db.internal'}

```

There is no `document[path]` to read or write, because `get()` returns a node and `set()` takes a plain value.

### `tree.parse(text)`

Parse a document into a `DocumentNode`. Raises the same `SOMLDecodeError` as `loads()` for the same input.

### `tree.unparse(document)`

Print a tree as text. It is named after `ast.unparse()`, but it keeps the source:

- A tree that was not changed prints its source exactly.
- A node that was not changed prints its source text, so `0x1F90`, `1_000_000`, `90m`, a block string, a quoted key, and an offset such as `+02:00` stay as they were written.
- A changed int keeps its radix (a negative one is written in decimal), and a changed string stays `'...'` or `"..."`, unless `'...'` cannot hold the new value. A changed block string is written on one line. When a block string began on the line after its key, it goes on the line of the key when it changes or a value that `set()` or a program makes replaces it, unless a comment is between them, so a formatted document stays formatted.
- A removed member or item takes the comments it owns, as `remove()` says, and its own lines when nothing else is on them. A comment on its own line above it stays. A value that `set()` replaces keeps the comments around it. A node that you put in a list yourself is a new node, so it gets the layout of the ones next to it, and a node moved to another container loses the comment at the end of its line, because comments belong to the source around a node, not to the node.
- A new member or item gets the layout of the ones next to it, and a node you made is written in canonical layout, but in your order. In a container that stays on one line, as `set()` describes, it is written on one line, as in `[1, 2, {a: 3}]`.
- Commas follow the "Editing" rules of the specification, so that an edit changes only the commas of what it adds or removes. A new member or item goes on its own line with no comma, after the comments that the one before it owns. When something other than its comma and those comments follows the one before it on its line, such as the `}` of `{a: 1}`, it goes on that line after a comma, and it has a comma after it when the one before has one. A removed member or item takes the comma after it. The removed ones at the end take the comma before them when the last of them has none and only spaces, tabs, and the comments that they own are between, so `[1, 2]` and `[1, /* note */ 2]` without `2` become `[1]`. Every other comma stays, so `[\n\t1,\n\t2\n]` without `2` keeps the comma after `1`, which is valid.
- A removed member or item does not leave a new blank line next to another one, directly inside a bracket, or at the start or end of the document. When it could take the blank line before it or the one after it, it takes the one before.

The output is always a valid document. A tree that does not make one, for example because an edit added a duplicate key, raises `ValueError`. The layout of what changed is best effort.

### `tree.walk(node)`

Yield the node and every node inside it, in source order: a node comes before its children, and a member's key before its value. It is like `ast.walk()`, but in source order, for tools such as linters. Comments and tokens are in `DocumentNode.comments` and `DocumentNode.tokens`.

```python
>>> [node.value for node in tree.walk(tree.parse('a: [1, 2]')) if isinstance(node, tree.IntegerNode)]
[1, 2]

```

### `tree.from_value(value)`

Make a node from a plain value, as `dumps()` would write it, and object members keep their order. Raises the same errors as `dumps()` for a value it cannot write. A scalar is fine, because a node is not a document.

### `tree.DocumentNode.from_value(value)`

Make a new document from a `dict` or a `list`, such as a starter config that a tool writes. It is written as `tree.from_value()` writes a node, and a top-level object has no braces. Raises the same errors as `dumps()`, including `TypeError` for a scalar.

A member that you make can have `comments`, which `unparse()` writes as `#` lines above it:

```python
>>> document = tree.DocumentNode.from_value({'name': 'api', 'port': 8080})
>>> document.body.members[1].comments = ['The port to listen on.']
>>> print(tree.unparse(document), end='')
name: 'api'
# The port to listen on.
port: 8080

```

`comments` works for a new member in a parsed document too, when the member starts its own line. `unparse()` raises `ValueError` for a comment with a line break, for a new member with comments that goes on the line of the text before it, as in `{a: 1}`, and for a member from the source with comments, because the comments of the source cannot be edited.

### Nodes

Every node is a mutable dataclass. A node from `parse()` has a `range`, which is `(start, end)` as indices into the source, and a node that you make has `range=None`.

| Node | Fields |
|---|---|
| `DocumentNode` | `body`: an `ObjectNode` or an `ArrayNode`; `comments`; `tokens`; `source`; `get()`, `set()`, `remove()`, `in`, and `del`; `position(offset)`, the 1-based line and column of an index |
| `ObjectNode` | `members`; `braced`, which is `False` only for a top-level object without braces. Changing it prints the object fresh, so the comments between and after its members go. |
| `MemberNode` | `key`; `value`; `comments`, the comment lines above a member that you make |
| `ArrayNode` | `elements` |
| `KeyNode` | `value`, decoded; `style`: `'bare'`, `'literal'`, or `'escaped'` |
| `StringNode` | `value`, decoded; `style`: `'literal'` or `'escaped'`; `block` |
| `IntegerNode` | `value`; `radix`: `2`, `8`, `10`, or `16` |
| `FloatNode` | `value`, including `inf` and `-inf` |
| `BooleanNode` | `value` |
| `NullNode` | |
| `DateNode` | `value`, a `datetime.date` (not a `datetime`) |
| `InstantNode` | `value`, a `datetime` in UTC |
| `DurationNode` | `value`, a `timedelta` |

The value nodes and `DocumentNode` have `to_value()`, which returns what `loads()` returns for that part of the document. `tree.ValueNode` is the union of the value nodes, and `tree.Node` the union of all nodes, for type annotations.

A `Comment` has `value` (without `#`, or without `/*` and `*/`), `kind` (`'line'` or `'block'`), and `range`. A `Token` has `value` (its source text), `kind` (`'punctuator'`, `'bare_key'`, `'string'`, `'integer'`, `'float'`, `'keyword'`, `'date'`, `'instant'`, or `'duration'`, where a quoted key is a `'string'`), and `range`. Comments and tokens are read-only: changing the lists changes nothing that `unparse()` writes.

## Recipes

### Typed config with msgspec

`msgspec.convert()` checks a plain value against a class. Pass `builtin_types` to keep dates, instants, and durations distinct from strings:

```python
from datetime import date, datetime, timedelta

import msgspec
import soml


class Server(msgspec.Struct):
	name: str
	port: int
	timeout: timedelta
	launch_date: date
	started: datetime


with open('config.soml', 'rb') as file:
	server = msgspec.convert(soml.load(file), Server, builtin_types=(date, datetime, timedelta))
```

It refuses `port: 8080.0`, because a float is not an int.

### Typed config with pydantic

Use strict mode, or pydantic accepts `3.0` and `'3'` for an `int`:

```python
from datetime import timedelta

import pydantic
import soml


class Config(pydantic.BaseModel):
	model_config = pydantic.ConfigDict(strict=True)
	name: str
	port: int
	timeout: timedelta


with open('config.soml', 'rb') as file:
	config = Config.model_validate(soml.load(file))
```

### Settings with pydantic-settings

A settings source that reads a SOML file, so that environment variables override it:

```python
from pathlib import Path

import soml
from pydantic_settings import BaseSettings, InitSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import ConfigFileSourceMixin


class SOMLConfigSettingsSource(InitSettingsSource, ConfigFileSourceMixin):
	def __init__(self, settings_class: type[BaseSettings], path: Path) -> None:
		super().__init__(settings_class, self._read_files(path))

	def _read_file(self, path: Path) -> dict[str, object]:
		# Binary mode, because text mode turns a carriage return into a line feed, which SOML must refuse.
		with path.open('rb') as file:
			value = soml.load(file)

		if not isinstance(value, dict):
			raise TypeError(f'{path} must hold an object, not an array')

		return value


class Settings(BaseSettings):
	model_config = SettingsConfigDict(env_prefix='APP_', strict=True)
	name: str
	port: int

	@classmethod
	def settings_customise_sources(
		cls,
		settings_cls: type[BaseSettings],
		init_settings: PydanticBaseSettingsSource,
		env_settings: PydanticBaseSettingsSource,
		dotenv_settings: PydanticBaseSettingsSource,
		file_secret_settings: PydanticBaseSettingsSource,
	) -> tuple[PydanticBaseSettingsSource, ...]:
		return (init_settings, env_settings, SOMLConfigSettingsSource(settings_cls, Path('config.soml')))


settings = Settings()  # `APP_PORT=9090` overrides `port: 8080` in the file.
```

### Compare two configs

Canonical form has one sorted member on each line, so a line diff shows every change, including a change of type, which comparing the values with `==` misses, because `3 == 3.0` in Python:

```python
>>> import difflib
>>> before = soml.dumps(soml.loads('a: 3\nb: 1'), canonical=True)
>>> after = soml.dumps(soml.loads('a: 3.0\nb: 1'), canonical=True)
>>> print(''.join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True))), end='')
---
+++
@@ -1,2 +1,2 @@
-a: 3
+a: 3.0
 b: 1

```

### Hash a config

Equal values give equal canonical text, so hash it to compare, cache, or sign a value. The comments, the order of members, and the spelling of values do not change the hash:

```python
>>> import hashlib
>>> hashlib.sha256(soml.dumps(soml.loads('b: 1\na: 0x10 # Note.'), canonical=True).encode()).hexdigest() == hashlib.sha256(soml.dumps(soml.loads('a: 16\nb: 1'), canonical=True).encode()).hexdigest()
True

```

## Limits

- **Nesting is limited to 100 levels**, as the specification requires, in reading, writing, and the tree. Every array and object is one level, including the document's own collection.
- **Microseconds**, not nanoseconds, for instants and durations (see above), except in `format()`.
- **Comments cannot be edited** through the tree, only added above a new member. Each comment belongs to the source around a child: the comment lines above it, and the comments that it owns, as `remove()` says. A run of block comments that has a line break inside one of them goes with one child as a whole, as in the Rust implementation.

## Benchmark

The time to read and write each of the shared [benchmark fixtures](https://github.com/soml-lang/soml/tree/main/benchmark), which says what each one measures. Each fixture holds the same data in SOML, JSON, and TOML, and each library reads its own format. Each time is the least CPU time of several runs, on an Apple M4 Pro with Python 3.14.7, on a busy machine, so the numbers are a rough guide. `orjson` is written in Rust, and the others are pure Python, except for the C accelerator of `json`.

| | soml | json | orjson | tomllib / tomli-w | tomlkit |
|---|---:|---:|---:|---:|---:|
| **Read** | | | | | |
| service (3 KB) | 136 µs | 10 µs | 4 µs | 198 µs | 1.7 ms |
| platform (35 KB) | 956 µs | 65 µs | 30 µs | 1.7 ms | 16 ms |
| earthquakes (733 KB) | 30 ms | 3.6 ms | 1.1 ms | 55 ms | 384 ms |
| **Write** | | | | | |
| service | 66 µs | 13 µs | 2 µs | 98 µs | 1.7 ms |
| platform | 444 µs | 74 µs | 12 µs | 716 µs | 13 ms |
| earthquakes | 16 ms | 3.1 ms | 441 µs | 25 ms | 468 ms |

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pyright
```

`HYPOTHESIS_PROFILE=thorough uv run pytest` runs the property tests with 10,000 examples each, and `uv run --group bench python bench/benchmark.py` measures the times in the table above, with `json`, `orjson`, `tomllib`, `tomli-w`, and `tomlkit`, from the shared fixtures in [`bench/fixtures`](https://github.com/soml-lang/soml-python/tree/main/bench/fixtures). They are a copy of the [`benchmark`](https://github.com/soml-lang/soml/tree/main/benchmark) directory of the `soml` repository. To update them, check out `soml` next to this repository and run `rsync --archive --delete ../soml/benchmark/ bench/fixtures/`.

The benchmark times calibrated batches with CPU time, with garbage collection disabled only during each batch, and reports the fastest batch's time per call. Narrow a run with `--fixtures service --libraries soml json --operations read`, and use `--seconds 0.05 --repeat 3` for a quicker check. `--json` returns timings in seconds under `timings`, together with Python, operating system, architecture, library versions, timer, and sampling settings under `environment`, so saved measurements can be compared with their context.

The conformance suite in [`tests/conformance`](https://github.com/soml-lang/soml-python/tree/main/tests/conformance) is a copy of the one in the [`soml`](https://github.com/soml-lang/soml/tree/main/conformance) spec repository. To update it, check out `soml` next to this repository and run `../soml/sync-conformance.sh tests/conformance`.
