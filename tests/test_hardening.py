"""Resource limits and trust boundaries for untrusted documents."""

import tracemalloc
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import soml
from soml import tree
from soml._parser import Parser


@pytest.mark.parametrize('base', [bytes, bytearray])
def test_invalid_utf8_diagnostics_do_not_call_overridden_byte_methods(base: type[bytes | bytearray]) -> None:
	class UntrustedBytes(base):  # type: ignore[misc,valid-type]
		def __getitem__(self, index: Any) -> Any:
			raise AssertionError('The decoder must use its own byte buffer')

		def decode(self, *_arguments: Any, **_options: Any) -> str:
			raise AssertionError('The decoder must use the builtin decoder')

	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(UntrustedBytes(b'a: 1\nb: \xff'))

	assert error_info.value.doc == 'a: 1\nb: '
	assert error_info.value.msg == 'Invalid UTF-8 byte 0xFF'
	assert (error_info.value.lineno, error_info.value.colno) == (2, 4)


@pytest.mark.parametrize('token', ['0.' + '3' * 150_000, '0b' + '0_' * 75_000 + '1', '1.' + '0' * 150_000 + 's'], ids=['float', 'binary', 'duration'])
def test_large_valid_numbers_do_not_allocate_backtracking_stacks(token: str) -> None:
	source = f'a: {token}'
	tracemalloc.start()

	try:
		result = soml.loads(source)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	assert isinstance(result, dict)
	assert peak < 4_000_000


def test_long_block_delimiters_cannot_amplify_error_messages() -> None:
	delimiter = "'" * 100_000
	with pytest.raises(soml.SOMLDecodeError) as error_info:
		soml.loads(f'a: {delimiter}\ntext {delimiter}')

	assert len(error_info.value.msg) < 2048
	assert error_info.value.msg.startswith('Unterminated block string')


def test_code_frame_only_allocates_the_visible_window() -> None:
	source = "a: '" + 'x' * 5_000_000 + "'\n!"
	tracemalloc.start()

	try:
		error = soml.SOMLDecodeError('Invalid value', source, len(source) - 1)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	assert '!' in error.code_frame
	assert peak < 100_000


def test_bulk_removal_preserves_comment_order_and_blank_boundaries() -> None:
	document = tree.parse('[\n\t# first\n\t1\n\n\t# second\n\t2\n\n\t# third\n\t3\n]')
	assert isinstance(document.body, tree.ArrayNode)
	document.body.elements.clear()
	assert tree.unparse(document) == '[\n\t# first\n\n\t# second\n\n\t# third\n]'


def test_duplicate_normalized_key_errors_stay_bounded() -> None:
	class IdentityKey(str):
		__slots__ = ()

		__hash__ = object.__hash__

		def __eq__(self, other: object) -> bool:
			return self is other

	key = 'x' * 100_000
	value: dict[str, soml.Serializable] = {IdentityKey(key): 1, IdentityKey(key): 2}

	for serialize in (soml.dumps, tree.DocumentNode.from_value):
		with pytest.raises(ValueError, match='two keys that are the same string') as error_info:
			serialize(value)

		assert 'two keys that are the same string' in str(error_info.value)
		assert len(str(error_info.value)) < 200


def test_overdeep_paths_are_rejected_before_building_missing_objects() -> None:
	document = tree.parse('stable: 1')
	path = ('missing',) * 20_000
	tracemalloc.start()

	try:
		with pytest.raises(ValueError, match='nested more than 100 levels'):
			document.set(path, 1)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	assert peak < 100_000
	assert tree.unparse(document) == 'stable: 1'


def test_position_cache_is_rebuilt_after_source_reassignment() -> None:
	document = tree.parse('a: 1\nb: 2')
	assert document.position(7) == (2, 3)
	document.source = 'a: 1\n\n\nb: 2\n'
	assert document.position(7) == (4, 1)
	assert document.position(len(document.source)) == (5, 1)


def test_shared_containers_build_independent_nodes_and_allow_independent_edits() -> None:
	shared = {'items': [1, 2]}
	document = tree.DocumentNode.from_value({'first': shared, 'second': shared})
	document.set(('first', 'items', 0), 9)
	assert document.to_value() == {'first': {'items': [9, 2]}, 'second': shared}
	assert soml.loads(tree.unparse(document)) == document.to_value()
	assert shared == {'items': [1, 2]}


@pytest.mark.parametrize('value', [1.0, 1e-7, 1e21, datetime(2026, 1, 1, tzinfo=UTC), timedelta(microseconds=-1)])
def test_changes_deep_in_a_parsed_tree_cannot_be_mistaken_for_unchanged_values(value: soml.Serializable) -> None:
	document = tree.parse('a: ' + '[' * 90 + '0' + ']' * 90)
	path: tuple[str | int, ...] = ('a', *([0] * 90))
	document.set(path, value)
	node = document.get(path)
	assert node is not None
	assert type(node.to_value()) is type(value)
	assert node.to_value() == value
	assert soml.loads(tree.unparse(document)) == document.to_value()


@pytest.mark.parametrize(
	('segment', 'expected_path'),
	[('a', 'scalar' + '.a' * 97 + '…'), ('😀', 'scalar' + '."😀"' * 38 + '."😀…')],
)
def test_long_path_errors_keep_utf16_abbreviation(segment: str, expected_path: str) -> None:
	path = ('scalar', *((segment,) * 10_000))
	document = tree.parse('scalar: 1')
	tracemalloc.start()

	try:
		with pytest.raises(ValueError, match='not an object or an array') as error_info:
			document.remove(path)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	assert str(error_info.value) == f'Cannot edit {expected_path}, because scalar is not an object or an array'
	assert peak < 100_000


def test_public_decode_errors_sanitize_messages_before_creating_exception_arguments() -> None:
	error = soml.SOMLDecodeError('\x1b[2J\u202esecret\x85', 'a: 1', 0)
	assert error.msg == '\ufffd[2J\ufffdsecret\ufffd'
	assert '\x1b' not in str(error)
	assert '\u202e' not in repr(error.args)


@pytest.mark.parametrize('operation', ['get', 'set', 'remove', 'contains', 'delete'])
def test_negative_index_subclasses_cannot_bypass_path_validation(operation: str) -> None:
	class Index(int):
		def __lt__(self, other: object) -> bool:
			return False

	document = tree.parse('[1, 2]')

	def run() -> None:
		match operation:
			case 'get':
				document.get(Index(-1))
			case 'set':
				document.set(Index(-1), 3)
			case 'remove':
				document.remove(Index(-1))
			case 'contains':
				_ = Index(-1) in document
			case 'delete':
				del document[Index(-1)]
			case _:
				raise AssertionError(operation)

	with pytest.raises(ValueError, match='non-empty array'):
		run()
	assert tree.unparse(document) == '[1, 2]'


def test_path_string_subclasses_use_their_string_value_for_lookup_and_editing() -> None:
	class Key(str):
		__slots__ = ()
		__hash__ = str.__hash__

		def __eq__(self, other: object) -> bool:
			raise AssertionError('Path comparison must use an exact string')

		def __str__(self) -> str:
			raise AssertionError('Path conversion must use the base method')

	document = tree.parse('parent: {child: 1}')
	path = (Key('parent'), Key('child'))
	assert path in document
	assert isinstance(document.get(path), tree.IntegerNode)
	document.set(path, 2)
	assert document.to_value() == {'parent': {'child': 2}}
	assert document.remove(path)
	assert tree.unparse(document) == 'parent: {}'


def test_thousands_separator_diagnostics_do_not_copy_or_reparse_an_enormous_number() -> None:
	source = '[1' + ',123' * 100_000 + ',000]'
	parser = Parser(source)
	start = len(source) - 4
	tracemalloc.start()
	try:
		reason = parser.describe_thousands_separator('000', start)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert reason is not None
	assert reason.endswith('is not one number')
	assert len(reason) < 200
	assert peak < 50_000


@pytest.mark.parametrize('prefix', ['0b', '0o', '0x'])
def test_radix_leading_zeros_do_not_allocate_a_normalized_copy(prefix: str) -> None:
	source = f'a: {prefix}' + '0_' * 100_000 + '1'
	tracemalloc.start()
	try:
		value = soml.loads(source)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert value == {'a': 1}
	# One padded source and one lexed token, without a further normalized token.
	assert peak < 450_000


def test_a_very_long_key_before_a_dot_has_bounded_diagnostic_work() -> None:
	source = '{' + 'k' * 1_000_000 + '.child: 1}'
	tracemalloc.start()
	try:
		with pytest.raises(soml.SOMLDecodeError, match='A key cannot contain') as error_info:
			soml.loads(source)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert error_info.value.pos == 1_000_001
	assert peak < 2_100_000


def test_unchanged_subtrees_are_rechecked_after_a_later_edit() -> None:
	source = 'outer: [\n' + '\t' + '[1, 2, 3],\n' + '\t0\n]\n'
	document = tree.parse(source)
	document.set(('outer', 1), 4)
	assert soml.loads(tree.unparse(document)) == {'outer': [[1, 2, 3], 4]}
	first = document.get(('outer', 0, 1))
	assert isinstance(first, tree.IntegerNode)
	first.value = 5
	assert tree.unparse(document) == 'outer: [\n\t[1, 5, 3],\n\t4\n]\n'


def test_long_whitespace_runs_keep_token_positions_and_comment_ownership() -> None:
	indentation = ' \t' * 10_000
	source = indentation + 'a: [' + indentation + '1, /* about two */' + indentation + '2\n]\n'
	document = tree.parse(source)
	assert document.to_value() == {'a': [1, 2]}
	assert tree.unparse(document) == source
	assert all(source[start:end] == token.value for token in document.tokens for start, end in [token.range])
	two = document.get(('a', 1))
	assert two is not None
	assert two.range is not None
	assert document.position(two.range[0]) == (1, source.index('2') + 1)
	assert document.remove(('a', 1))
	assert 'about two' not in tree.unparse(document)
	assert soml.loads(tree.unparse(document)) == {'a': [1]}
	assert soml.format(source) == 'a: [\n\t1\n\t/* about two */ 2\n]\n'


def test_serializing_a_large_nested_range_does_not_buffer_all_items() -> None:
	value = [range(30_000)]
	tracemalloc.start()
	try:
		text = soml.dumps(value)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()
	assert soml.loads(text) == [list(range(30_000))]
	assert peak < 3_000_000
