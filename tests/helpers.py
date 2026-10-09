"""Helpers shared by the test files."""

import time
import timeit

import soml

TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable


def best_time(run: Callable[[], object], runs: int = 5) -> float:
	"""
	The best CPU time of several runs, so that a busy machine does not fail a timing test: CPU time does not count the time that other processes run. The garbage collector is off, because a collection takes time in proportion to everything that is alive, not to the work being measured. `timeit` preserves the caller's collection setting even when the workload raises.
	"""
	return min(timeit.Timer(run, timer=time.process_time).repeat(repeat=runs, number=1))


def typed(value: object) -> object:
	"""Pairs every scalar with its type, because `1 == 1.0 == True` in Python."""
	if isinstance(value, list):
		return [typed(item) for item in value]

	if isinstance(value, dict):
		return {key: typed(member) for key, member in value.items()}

	return (type(value).__name__, value)


def value_paths(value: object, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
	"""Every path in a plain value, including the paths of the containers."""
	paths: list[tuple[str | int, ...]] = [prefix] if prefix else []

	if isinstance(value, dict):
		for key, member in value.items():
			paths += value_paths(member, (*prefix, key))
	elif isinstance(value, list):
		for index, item in enumerate(value):
			paths += value_paths(item, (*prefix, index))

	return paths


def load_object(text: str | bytes) -> dict[str, soml.Value]:
	"""Parses a document that must be an object."""
	value = soml.loads(text)
	assert isinstance(value, dict)
	return value


def load_value(source: str) -> soml.Value:
	"""Parses `a: <source>` and returns the value."""
	return load_object(f'a: {source}')['a']


def decode_error(text: str | bytes) -> soml.SOMLDecodeError:
	"""Parses a document that must be rejected, and returns the error."""
	try:
		soml.loads(text)
	except soml.SOMLDecodeError as error:
		return error

	raise AssertionError(f'Expected {text!r} to be rejected')


# The example documents from the readme and the comparison in the SOML spec repository. They are copied, so that the tests do not depend on a sibling folder.

EVERY_FEATURE_DOCUMENT = r"""/*
Everything SOML can express, in one document. A document may also be a top-level array.
*/

# Keys are bare. Dashes are allowed, so no key needs quotes.
name: 'soml'
launch-date: 2026-09-19
released-at: 2026-09-19T14:00:00Z
'the name': 'quotes are the escape hatch, when a key needs spaces'
404: 'and a key may begin with a digit'

# Strings: '...' is literal, "..." is escaped. Use ' by default.
regex: '^\d{4}-\d{2}-\d{2}$'
windows-path: 'C:\Users\sindre\dev'
shell: 'echo "$HOME" | tr -d "\n"'
escaped: "line one\nline two\t\"quoted\"\u{1f600}"

# Block strings are dedented by the closing delimiter's column.
description: '''
	A block string keeps its line breaks.
	The leading tab of each line is stripped,
	because the closing delimiter sits at one tab.
	'''

# Numbers: int and float are different types.
replicas: 3
ratio: 0.75
budget: 1_000_000
mask: 0b1010_1100
permissions: 0o644
color: 0xFF8800
flags: 0x00FF
limit: infinity
floor: -infinity

# Durations: exact, in h, m, s, ms, us, and ns.
retry-after: 1m30s

# Booleans and null.
drained: false
owner: null

# Arrays. A comma or a line break separates items.
labels: ['prod', 'eu-west', 'canary']
matrix: [[1, 2], [3, 4]]
mixed: [1, 'two', true, null]

# Objects. The top-level object needs no braces.
pool: {
	min: 2
	max: 16
}

# Records in arrays. Braces are required, and that is deliberate.
maintainers: [
	{name: 'Sindre Sorhus', email: 'sindresorhus@gmail.com'}
	{name: 'Random Person', email: 'person@example.com'}
]

# Line comments, and block comments, anywhere.
# Inline objects and one-line braced documents work too:
healthcheck: {path: '/health', interval: 10s, grace: 1.5s}
"""

EDGE_SERVICE_DOCUMENT = r"""# The edge service. See the runbook before you touch this.
name: 'api-gateway'
version: '2.1.0'
pattern: '^\d{4}-\d{2}-\d{2}$'
windows-path: 'C:\Users\sindre\dev'
content-security-policy: "default-src 'self'; script-src https://cdn.example.com"
replicas: 3
timeout: 30s
drained: false
owner: null
deployed-at: 2026-09-19T14:00:00Z
labels: ['prod', 'eu-west', 'canary']
description:
	'''
	The API gateway terminates TLS.
	It routes requests to internal services.
	It has no external dependencies.
	'''
postgres: {
	host: 'db.internal'
	port: 5432
	ssl: true
}
"""
