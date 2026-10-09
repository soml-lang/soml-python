import pytest

import soml
from soml import tree

TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Callable


@pytest.mark.parametrize('value', [None, 0, 'value', [], {}])
def test_walk_rejects_values_instead_of_nodes(value: object) -> None:
	with pytest.raises(TypeError, match='Expected a node'):
		list(tree.walk(value))  # type: ignore[arg-type]


@pytest.mark.parametrize('container', [tree.ArrayNode([0]), tree.ObjectNode([0])])  # type: ignore[list-item]
def test_walk_rejects_nonnode_children(container: tree.ArrayNode | tree.ObjectNode) -> None:
	iterator = tree.walk(container)
	assert next(iterator) is container
	with pytest.raises(TypeError, match='Expected a node'):
		next(iterator)


def test_deep_edit_does_not_recheck_each_descendant_for_each_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
	document = tree.parse('[' * 90 + '1' + ']' * 90)
	nodes = list(tree.walk(document))
	leaf = nodes[-1]
	assert isinstance(leaf, tree.IntegerNode)
	leaf.value = 2
	calls = 0
	original = tree.IntegerNode._snapshot

	def counted_snapshot(node: tree.IntegerNode) -> tuple[object, ...]:
		nonlocal calls
		calls += 1
		return original(node)

	monkeypatch.setattr(tree.IntegerNode, '_snapshot', counted_snapshot)
	assert tree.unparse(document) == '[' * 90 + '2' + ']' * 90
	assert calls <= 3


def test_equal_nodes_are_still_distinct_when_reordering_source_children() -> None:
	class EqualInteger(tree.IntegerNode):
		__slots__ = ()
		__hash__ = None  # type: ignore[assignment] # Equality deliberately ignores identity in this test.

		def __eq__(self, other: object) -> bool:
			return isinstance(other, tree.IntegerNode)

	document = tree.parse('[0xA, 10 /* decimal */]')
	assert isinstance(document.body, tree.ArrayNode)
	first, second = document.body.elements
	# These parsed nodes compare equal after a class change, but each keeps its own spelling and comments.
	first.__class__ = EqualInteger
	second.__class__ = EqualInteger
	document.body.elements.reverse()
	assert tree.unparse(document) == '[10 /* decimal */, 0xA, ]'
	assert soml.loads(tree.unparse(document)) == [10, 10]


@pytest.mark.parametrize('operation', [lambda document: document.set('a', 2), lambda document: document.remove('a')])
def test_external_member_list_remains_the_same_object_after_edit(operation: Callable[[tree.DocumentNode], object]) -> None:
	document = tree.parse('a: 1\nb: 3\n')
	assert isinstance(document.body, tree.ObjectNode)
	members = document.body.members
	operation(document)
	assert members is document.body.members
	assert document.to_value() == soml.loads(tree.unparse(document))


def test_each_unparse_uses_current_mutations_without_stale_changed_state() -> None:
	document = tree.parse('outer: [{n: 0xA}, {n: 2}] # keep\n')
	value = document.get(('outer', 0, 'n'))
	assert isinstance(value, tree.IntegerNode)
	value.value = 11
	assert tree.unparse(document) == 'outer: [{n: 0xB}, {n: 2}] # keep\n'
	value.value = 10
	assert tree.unparse(document) == document.source
	value.value = 12
	assert tree.unparse(document) == 'outer: [{n: 0xC}, {n: 2}] # keep\n'


@pytest.mark.parametrize('source', ['[]', '{}', 'outer: []', 'outer: {}'])
def test_editing_an_originally_empty_container_is_detected(source: str) -> None:
	document = tree.parse(source)
	container = document.get('outer') if source.startswith('outer:') else document.body
	if isinstance(container, tree.ObjectNode):
		container.members.append(tree.MemberNode(tree.KeyNode('key'), tree.IntegerNode(5)))
	else:
		assert isinstance(container, tree.ArrayNode)
		container.elements.append(tree.IntegerNode(5))
	assert soml.loads(tree.unparse(document)) == document.to_value()
	assert tree.unparse(document) != source


def test_replacing_a_child_with_a_different_parsed_spelling_does_not_reuse_old_source() -> None:
	document = tree.parse('[0xA, 0xB]')
	replacement = tree.parse('[10]').get(0)
	assert isinstance(document.body, tree.ArrayNode)
	assert replacement is not None
	document.body.elements[0] = replacement
	assert tree.unparse(document) == '[10, 0xB]'


def test_a_shared_value_can_be_converted_and_walked_once_per_branch() -> None:
	shared: dict[str, soml.Serializable] = {'enabled': True, 'ports': [80, 443]}
	document = tree.DocumentNode.from_value([shared, shared])
	assert document.to_value() == [shared, shared]
	assert soml.loads(tree.unparse(document)) == [shared, shared]
	assert sum(isinstance(node, tree.BooleanNode) for node in tree.walk(document)) == 2


@pytest.mark.parametrize('path', [False, True, -1])
def test_single_path_fast_paths_preserve_invalid_index_errors(path: int) -> None:
	document = tree.parse('[1]')
	expected_error = TypeError if isinstance(path, bool) else ValueError
	with pytest.raises(expected_error, match='non-empty array'):
		document.get(path)
	with pytest.raises(expected_error, match='non-empty array'):
		document.set(path, 2)
	with pytest.raises(expected_error, match='non-empty array'):
		document.remove(path)
	assert tree.unparse(document) == '[1]'


@pytest.mark.parametrize(('offset', 'column'), [(False, 1), (True, 2)])
def test_positions_keep_boolean_index_semantics(*, offset: bool, column: int) -> None:
	document = tree.parse('a: 1')
	assert document.position(offset) == (1, column)
