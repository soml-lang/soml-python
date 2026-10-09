import pytest

from soml import tree


@pytest.mark.parametrize('child', [tree.KeyNode('x'), tree.MemberNode(tree.KeyNode('x'), tree.NullNode()), tree.DocumentNode(tree.ArrayNode())])
def test_array_conversion_rejects_structural_nodes(child: tree.Node) -> None:
	node = tree.ArrayNode([child])  # type: ignore[list-item]
	with pytest.raises(TypeError, match='Expected a value node'):
		node.to_value()


@pytest.mark.parametrize('child', [tree.KeyNode('x'), tree.MemberNode(tree.KeyNode('x'), tree.NullNode()), tree.DocumentNode(tree.ArrayNode())])
def test_object_conversion_rejects_structural_values(child: tree.Node) -> None:
	node = tree.ObjectNode([tree.MemberNode(tree.KeyNode('x'), child)])  # type: ignore[arg-type]
	with pytest.raises(TypeError, match='Expected a value node'):
		node.to_value()


@pytest.mark.parametrize('child', [tree.KeyNode('x'), tree.MemberNode(tree.KeyNode('x'), tree.NullNode()), tree.DocumentNode(tree.ArrayNode())])
@pytest.mark.parametrize('parsed', [False, True])
def test_array_printing_rejects_structural_values(child: tree.Node, *, parsed: bool) -> None:
	document = tree.parse('[0]') if parsed else tree.DocumentNode(tree.ArrayNode([tree.IntegerNode(0)]))
	assert isinstance(document.body, tree.ArrayNode)
	document.body.elements[0] = child  # type: ignore[assignment]
	with pytest.raises(TypeError, match='Expected a value node'):
		tree.unparse(document)


@pytest.mark.parametrize('body', [tree.KeyNode('x'), tree.MemberNode(tree.KeyNode('x'), tree.NullNode()), tree.StringNode('x')])
def test_printing_rejects_a_scalar_or_structural_document_body(body: tree.Node) -> None:
	document = tree.DocumentNode(body)  # type: ignore[arg-type]
	with pytest.raises(TypeError, match='Expected an ObjectNode or ArrayNode'):
		tree.unparse(document)


@pytest.mark.parametrize('parsed', [False, True])
def test_printing_rejects_an_object_member_without_a_key_node(*, parsed: bool) -> None:
	member = tree.MemberNode(tree.StringNode('x'), tree.NullNode())  # type: ignore[arg-type]
	node = tree.parse('x: 1') if parsed else tree.ObjectNode([member])
	if isinstance(node, tree.DocumentNode):
		assert isinstance(node.body, tree.ObjectNode)
		node.body.members[0].key = member.key
		document = node
	else:
		document = tree.DocumentNode(node)
	with pytest.raises(TypeError, match='Expected a KeyNode'):
		tree.unparse(document)


def test_walk_rejects_a_member_cycle() -> None:
	member = tree.MemberNode(tree.KeyNode('x'), tree.NullNode())
	member.value = member  # type: ignore[assignment]
	iterator = tree.walk(member)
	assert next(iterator) is member
	assert isinstance(next(iterator), tree.KeyNode)
	with pytest.raises(ValueError, match='circular'):
		next(iterator)


def test_walk_does_not_copy_a_wide_containers_children() -> None:
	import tracemalloc  # noqa: PLC0415 # The allocation measurement is isolated to this test.

	node = tree.ArrayNode([tree.NullNode()] * 20_000)
	tracemalloc.start()
	try:
		iterator = tree.walk(node)
		assert next(iterator) is node
		assert next(iterator) is node.elements[0]
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	assert peak < 200_000


@pytest.mark.parametrize('braced', [1, 1.0])
def test_boolean_like_braced_values_do_not_bypass_parsed_validation(braced: object) -> None:
	document = tree.parse('{x: 1}')
	assert isinstance(document.body, tree.ObjectNode)
	document.body.braced = braced  # type: ignore[assignment]
	with pytest.raises(TypeError, match='holds a bool for braced'):
		tree.unparse(document)


@pytest.mark.parametrize('radix', [10.0, 16.0, True])
def test_numeric_like_radices_do_not_bypass_parsed_validation(radix: object) -> None:
	document = tree.parse('x: 0x10' if radix == 16 else 'x: 10')
	node = document.get('x')
	assert isinstance(node, tree.IntegerNode)
	node.radix = radix  # type: ignore[assignment]
	with pytest.raises(ValueError, match='The radix of an IntegerNode'):
		tree.unparse(document)


def test_an_unchanged_replacement_body_keeps_its_source() -> None:
	document = tree.parse('# Header\nx: 1\n# Trailer\n')
	document.body = tree.parse('[0x10, 2 /* item */]').body
	assert tree.unparse(document) == '# Header\n[0x10, 2 /* item */]\n'


def test_invalid_nested_layout_types_cannot_hide_in_an_unchanged_parent() -> None:
	document = tree.parse('[{x: 1}]')
	assert isinstance(document.body, tree.ArrayNode)
	nested = document.body.elements[0]
	assert isinstance(nested, tree.ObjectNode)
	nested.braced = 1  # type: ignore[assignment]
	with pytest.raises(TypeError, match='holds a bool for braced'):
		tree.unparse(document)


@pytest.mark.parametrize('parsed', [False, True])
def test_object_printing_rejects_nonmember_children(*, parsed: bool) -> None:
	document = tree.parse('{x: 0}') if parsed else tree.DocumentNode(tree.ObjectNode())
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members[:] = [tree.StringNode('x')]  # type: ignore[list-item]
	with pytest.raises(TypeError, match='Expected a MemberNode'):
		tree.unparse(document)


@pytest.mark.parametrize('parsed', [False, True])
def test_member_printing_rejects_structural_values(*, parsed: bool) -> None:
	document = tree.parse('x: 0') if parsed else tree.DocumentNode.from_value({'x': 0})
	assert isinstance(document.body, tree.ObjectNode)
	document.body.members[0].value = tree.KeyNode('x', 'literal')  # type: ignore[assignment]
	with pytest.raises(TypeError, match='Expected a value node'):
		tree.unparse(document)
