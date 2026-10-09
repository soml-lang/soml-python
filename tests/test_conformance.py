"""
Runs the language-neutral conformance suite in `conformance/`, which is a copy of the suite in the `soml` spec repository. Edit it there, and update the copy with `../soml/sync-conformance.sh tests/conformance`.

- `valid/**/name.soml` must parse to the tagged value in `name.json`, serialize with `canonical=True` to exactly `name.canonical.soml`, and format to exactly `name.formatted.soml`.
- `invalid/**/name.soml` must be rejected, with the reason, line, and column in `conformance/invalid-reasons.json`.
- `edit/name.json` holds a formatted `document`, a `path`, a tagged `value`, which is left out for a removal, and the `expected` document after `DocumentNode.set()` or `remove()` makes that change. A removal of a value that is not there expects the `document` unchanged. A case with `"error": true` has no `expected`, because the change must fail, as it does for a path that does not fit the document.

Every file is read as bytes, because text mode would turn a carriage return into a line feed, and the carriage return cases would test the wrong input.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from helpers import typed

import soml
from soml import tree

ROOT = Path(__file__).parent / 'conformance'

INVALID_REASONS: dict[str, dict[str, object]] = json.loads((ROOT / 'invalid-reasons.json').read_bytes())


# Valid documents with an instant or a duration finer than a microsecond, which `datetime` and `timedelta` cannot hold, so `loads()` rejects them. This is a documented deviation from the spec. `format()` accepts them, so `test_formatted()` runs them too. Found from the expected values, so that a copy of a newer corpus cannot leave this list behind.
def is_finer_than_a_microsecond(tagged: object) -> bool:
	if isinstance(tagged, list):
		return any(is_finer_than_a_microsecond(item) for item in tagged)

	assert isinstance(tagged, dict)

	match tagged:
		case {'type': 'duration', 'value': str(value)}:
			return int(value) % 1000 != 0
		case {'type': 'instant', 'value': str(value)}:
			fraction = value.removesuffix('Z').partition('.')[2]
			return len(fraction.rstrip('0')) > 6
		case {'type': str(), 'value': str()}:
			return False
		case _:
			return any(is_finer_than_a_microsecond(member) for member in tagged.values())


SUB_MICROSECOND_CASES = {path.relative_to(ROOT / 'valid').with_suffix('').as_posix() for path in (ROOT / 'valid').rglob('*.json') if is_finer_than_a_microsecond(json.loads(path.read_bytes()))}


def list_cases(directory: str) -> list[str]:
	base = ROOT / directory
	return sorted(path.relative_to(base).with_suffix('').as_posix() for path in base.rglob('*.soml') if not path.name.endswith(('.canonical.soml', '.formatted.soml')))


VALID_CASES = list_cases('valid')
INVALID_CASES = list_cases('invalid')
EDIT_CASES = sorted(path.stem for path in (ROOT / 'edit').glob('*.json'))


def untag(tagged: object) -> object:
	"""Turns the tagged JSON of the suite into the values this package returns."""
	if isinstance(tagged, list):
		return [untag(item) for item in tagged]

	assert isinstance(tagged, dict)

	if tagged.keys() == {'type', 'value'} and isinstance(tagged['type'], str) and isinstance(tagged['value'], str):
		value: str = tagged['value']

		match tagged['type']:
			case 'string':
				return value
			case 'int':
				return int(value)
			case 'float':
				return float(value)
			case 'bool':
				return value == 'true'
			case 'null':
				return None
			case 'date':
				return date.fromisoformat(value)
			case 'instant':
				return datetime.fromisoformat(value).astimezone(UTC)
			case 'duration':
				return timedelta(microseconds=int(value) // 1000)
			case _:
				raise AssertionError(f'Unknown tag {tagged["type"]}')

	return {key: untag(member) for key, member in tagged.items()}


def decodes_as_utf8(data: bytes) -> bool:
	try:
		data.decode()
	except UnicodeDecodeError:
		return False

	return True


def test_the_suite_is_not_empty() -> None:
	assert len(VALID_CASES) > 300
	assert len(INVALID_CASES) > 300
	assert len(EDIT_CASES) > 40


def test_every_invalid_case_has_a_reason() -> None:
	assert sorted(INVALID_REASONS) == INVALID_CASES


def test_every_sub_microsecond_case_exists() -> None:
	assert set(VALID_CASES) >= SUB_MICROSECOND_CASES


@pytest.mark.parametrize('name', [name for name in VALID_CASES if name not in SUB_MICROSECOND_CASES])
def test_valid(name: str) -> None:
	base = ROOT / 'valid' / name
	data = base.with_name(f'{base.name}.soml').read_bytes()
	expected = typed(untag(json.loads(base.with_name(f'{base.name}.json').read_bytes())))
	canonical = base.with_name(f'{base.name}.canonical.soml').read_bytes().decode()

	assert typed(soml.loads(data)) == expected
	from_string = soml.loads(data.decode())
	assert typed(from_string) == expected
	assert soml.dumps(from_string, canonical=True) == canonical

	# Canonical form is a valid document with the same value, and a fixed point.
	assert typed(soml.loads(canonical)) == expected
	assert soml.dumps(soml.loads(canonical), canonical=True) == canonical

	# The default differs from canonical form only in member order, and the members of canonical form are already sorted.
	assert soml.dumps(soml.loads(canonical)) == canonical

	# The formatted form is a valid document with the same value.
	assert typed(soml.loads(base.with_name(f'{base.name}.formatted.soml').read_bytes())) == expected


@pytest.mark.parametrize('name', VALID_CASES)
def test_formatted(name: str) -> None:
	base = ROOT / 'valid' / name
	formatted = base.with_name(f'{base.name}.formatted.soml').read_bytes().decode()

	assert soml.format(base.with_name(f'{base.name}.soml').read_bytes()) == formatted

	# The formatted form is a fixed point.
	assert soml.format(formatted) == formatted


@pytest.mark.parametrize('name', sorted(SUB_MICROSECOND_CASES))
def test_valid_but_finer_than_a_microsecond(name: str) -> None:
	data = (ROOT / 'valid' / f'{name}.soml').read_bytes()

	with pytest.raises(soml.SOMLDecodeError, match=r"finer than a microsecond, which Python's (datetime|timedelta) cannot hold"):
		soml.loads(data)


@pytest.mark.parametrize('name', INVALID_CASES)
def test_invalid(name: str) -> None:
	data = (ROOT / 'invalid' / f'{name}.soml').read_bytes()

	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(data)

	error = error_info.value
	assert {'reason': error.msg, 'line': error.lineno, 'column': error.colno} == INVALID_REASONS[name]

	if decodes_as_utf8(data):
		with pytest.raises(soml.SOMLDecodeError):
			soml.loads(data.decode())


@pytest.mark.parametrize('name', EDIT_CASES)
def test_edit(name: str) -> None:
	case = json.loads((ROOT / 'edit' / f'{name}.json').read_bytes())
	document_text: str = case['document']
	assert soml.format(document_text) == document_text, 'The document is not formatted.'

	document = tree.parse(document_text)
	path = tuple(case['path'])

	def change() -> bool | None:
		"""Makes the change, and returns whether `remove()` removed something, or `None` for `set()`."""
		if 'value' in case:
			document.set(path, untag(case['value']))  # type: ignore[arg-type] # The tagged JSON holds only values that `set()` accepts.
			return None

		return document.remove(path)

	if case.get('error'):
		with pytest.raises(ValueError, match=r'^Cannot edit '):
			change()

		assert tree.unparse(document) == document_text, 'A change that fails leaves the document unchanged.'
		return

	expected: str = case['expected']
	removed = change()

	# Removing what is not there changes nothing, and `remove()` tells.
	if removed is not None:
		assert removed == (expected != document_text)

	assert tree.unparse(document) == expected
	assert soml.format(expected) == expected, 'The result is not formatted.'
