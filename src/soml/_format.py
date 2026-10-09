"""
The formatter from the spec: it normalizes the layout of a document and keeps everything else the author wrote. A port of `format()` in the JS reference implementation, soml-js, so the two give the same text.
"""

import re
from itertools import chain

from ._parser import MAX_DEPTH, skip_spaces
from .tree import ArrayNode, Comment, DocumentNode, KeyNode, MemberNode, ObjectNode, StringNode, ValueNode, _parse  # pyright: ignore[reportPrivateUsage] # Private to the package, not to `soml.tree`.

TYPE_CHECKING = False

if TYPE_CHECKING:
	from collections.abc import Sequence

INDENT = '\t'
# A container and a value moved below a colon comment can each add one indentation level. Block strings can add one more.
LINE_INDENTATIONS = tuple(f'\n{INDENT * level}' for level in range(2 * MAX_DEPTH + 2))
BLANK_LINE_BETWEEN = re.compile(r'\n[\t ]*+\n')
CONTAINER_NODES = (ObjectNode, ArrayNode)


def format_document(text: str | bytes | bytearray) -> str:
	# A value finer than a microsecond is valid, and the formatter never reads a value, so it formats every document that the spec accepts.
	return Printer(_parse(text, truncates_nanoseconds=True)).print_document()


def node_range(node: MemberNode | ValueNode | KeyNode) -> tuple[int, int]:
	"""The range of a node from `parse()`, which always has one."""
	assert node.range is not None  # noqa: S101 # Every node from `parse()` has a range.
	return node.range


class Printer:
	"""Writes the nodes of a parsed document with the normalized layout, and copies everything else from the source."""

	__slots__ = ('body', 'comment_index', 'comments', 'parts', 'text')

	def __init__(self, document: DocumentNode) -> None:
		self.text = document.source
		self.body = document.body
		self.comments = document.comments
		self.parts: list[str] = []
		self.comment_index = 0

	def new_line(self, level: int, *, is_blank: bool) -> None:
		"""Starts a new line at `level`, with one blank line before it when the source had one."""
		if not self.parts:
			return

		if is_blank:
			self.parts.append('\n')
		self.parts.append(LINE_INDENTATIONS[level])

	def is_same_line(self, start: int, end: int) -> bool:
		return self.text.find('\n', start, end) == -1

	def has_blank_line(self, start: int, end: int) -> bool:
		return BLANK_LINE_BETWEEN.search(self.text, start, end) is not None

	def peek_comment(self, end: int) -> Comment | None:
		"""The next comment that ends at or before `end`, without consuming it."""
		comments = self.comments

		if self.comment_index < len(comments) and comments[self.comment_index].range[1] <= end:
			return comments[self.comment_index]

		return None

	def write_comment(self, comment: Comment) -> None:
		self.comment_index += 1

		if comment.kind == 'line':
			self.parts.append(f'#{comment.value.rstrip(" \t")}')
			return

		if '\n' not in comment.value:
			self.parts.append(f'/*{comment.value}*/')
			return

		# The lines of a block comment keep their own indentation. Trailing whitespace is removed, and runs of blank lines collapse to one, as everywhere outside a block string.
		lines = comment.value.split('\n')
		lines[0] = f'/*{lines[0]}'
		lines[-1] = f'{lines[-1]}*/'
		lines = [line.rstrip(' \t') for line in lines]
		# An empty line is kept only after a line that is not empty, so a run of them becomes one. The first line holds the `/*`, so `lines[-1]` is never read for an empty line.
		# Note: Not using `itertools.groupby()` because grouping blank runs was slower and required extra iterator layers.
		self.parts.append('\n'.join(line for index, line in enumerate(lines) if line or lines[index - 1]))

	def find_comma(self, start: int, end: int) -> int | None:
		"""The offset of the `,` after an item in the source, or `None` when there is none."""
		index = self.text.find(',', start, end)
		if index == -1:
			return None

		# The builder already recorded the comments. Skip their ranges without copying the gap or replacing its text.
		for comment_index in range(self.comment_index, len(self.comments)):
			comment_start, comment_end = self.comments[comment_index].range
			if index == -1 or comment_start > index:
				break
			if index < comment_end:
				index = self.text.find(',', comment_end, end)

		return None if index == -1 else index

	def write_list(self, items: Sequence[MemberNode | ValueNode], start: int, end: int, level: int, *, is_bracketed: bool) -> None:
		"""
		Writes `items` one per line at `level`, with the comments between them, from `start` to `end` in the source.

		A comment on the line of the item before it, or of the opening bracket, stays at the end of that line. The exception is a block comment after the comma that the next item follows on the same line, as in `1, /* note */ 2`: it stays in front of that item. Every other comment gets its own line.
		"""
		previous_end = start
		# Whether a comment may stay at the end of the line before it, which needs an item or an opening bracket there.
		can_trail = is_bracketed
		has_written = False
		is_on_same_line = False

		for item in chain(items, (None,)):
			item_start, item_end = (end, end) if item is None else node_range(item)
			comment = self.peek_comment(item_start)
			comma = self.find_comma(previous_end, item_start) if is_bracketed and item is not None and comment is not None else None
			# Where the item's line starts, or 0 when no line break comes between the item before it and this one. Found once per item rather than once per comment, which would be quadratic in the number of comments on one line.
			item_line_start = None if not can_trail or item is None or comment is None else self.text.rfind('\n', previous_end, item_start) + 1

			while comment is not None:
				comment_start, comment_end = comment.range
				is_trailing = can_trail and not is_on_same_line and self.is_trailing(comment, previous_end, item_line_start, comma)

				if is_trailing or is_on_same_line:
					self.parts.append(' ')
				else:
					self.new_line(level, is_blank=has_written and self.has_blank_line(previous_end, comment_start))

				self.write_comment(comment)
				has_written = has_written or not is_trailing
				# A block comment that the next comment or item follows on the same line keeps it on that line.
				next_comment = self.peek_comment(item_start)
				next_start = item_start if next_comment is None else next_comment.range[0]
				is_on_same_line = comment.kind == 'block' and not is_trailing and self.is_same_line(comment_end, next_start)
				previous_end = comment_end
				comment = next_comment

			if item is None:
				return

			if is_on_same_line:
				self.parts.append(' ')
			else:
				self.new_line(level, is_blank=has_written and self.has_blank_line(previous_end, item_start))

			self.item(item, level)
			previous_end = item_end
			can_trail = True
			has_written = True
			is_on_same_line = False

	def is_trailing(self, comment: Comment, previous_end: int, item_line_start: int | None, comma: int | None) -> bool:
		"""Whether a comment stays at the end of the line before it. That line ends at the item before it, or at the comma after that item."""
		comment_start, comment_end = comment.range

		# A block comment after the comma that the next item follows on the same line stays in front of that item. The comment ends on the item's line when it ends after the start of that line.
		if item_line_start is not None and comment.kind == 'block' and (comma is None or comment_start > comma) and comment_end >= item_line_start:
			return False

		# A comma is on the line of the item before it, so a comment after the comma that is on that line stays at its end.
		return self.is_same_line(previous_end, comment_start)

	def one_line(self, items: Sequence[MemberNode | ValueNode], start: int, end: int, level: int) -> None:
		"""Writes a container that is on one line in the source, from `start` to `end`, on one line, with a comma and a space between its members or items and no trailing comma. Only a block comment can be inside it, and each one stays where it is among the items and commas."""
		self.parts.append(self.text[start])
		# With no comments in the container, every separator is simply a comma and a space. The parser already checked where the commas are.
		if self.peek_comment(end) is None:
			for index, child in enumerate(items):
				if index:
					self.parts.append(', ')
				self.item(child, level)
			self.parts.append(self.text[end - 1])
			return

		previous_end = start + 1
		has_written_token = False

		for item in chain(items, (None,)):
			item_start, item_end = (end - 1, end - 1) if item is None else node_range(item)
			# The comma after the last item is left out.
			comma = self.find_comma(previous_end, item_start) if has_written_token and item is not None else None
			has_written_comma = False
			comment = self.peek_comment(item_start)

			while comment is not None:
				if not has_written_comma and comma is not None and comma < comment.range[0]:
					self.parts.append(',')
					has_written_comma = True

				if has_written_token:
					self.parts.append(' ')
				self.write_comment(comment)
				has_written_token = True
				comment = self.peek_comment(item_start)

			if item is None:
				break

			if comma is not None and not has_written_comma:
				self.parts.append(', ')
			elif has_written_token:
				self.parts.append(' ')

			self.item(item, level)
			previous_end = item_end
			has_written_token = True

		self.parts.append(self.text[end - 1])

	def item(self, item: MemberNode | ValueNode, level: int) -> None:
		if isinstance(item, MemberNode):
			self.member(item, level)
		else:
			self.value(item, level)

	def member(self, member: MemberNode, level: int) -> None:
		key_start, key_end = node_range(member.key)
		value_start = node_range(member.value)[0]
		self.parts.append(self.text[key_start : key_end + 1])
		is_block_string = isinstance(member.value, StringNode) and member.value.block
		comment = self.peek_comment(value_start)

		if comment is None:
			# A block string begins on the next line, one level deeper, so its delimiters and content line up.
			if is_block_string:
				self.new_line(level + 1, is_blank=False)
				level += 1
			else:
				self.parts.append(' ')
			self.value(member.value, level)
			return

		# With comments between the `:` and the value, the line breaks between them are kept, and a value on a new line is one level deeper.
		previous_end = key_end + 1
		value_level = level
		while comment is not None:
			if not self.write_separator(previous_end, comment.range[0], level + 1):
				value_level = level + 1

			self.write_comment(comment)
			previous_end = comment.range[1]
			comment = self.peek_comment(value_start)

		if is_block_string:
			self.new_line(level + 1, is_blank=False)
			value_level = level + 1
		elif not self.write_separator(previous_end, value_start, level + 1):
			value_level = level + 1

		self.value(member.value, value_level)

	def write_separator(self, start: int, end: int, level: int) -> bool:
		"""Write a separator and report whether `end` is on the same source line as `start`."""
		is_same_line = self.is_same_line(start, end)
		if is_same_line:
			self.parts.append(' ')
		else:
			self.new_line(level, is_blank=False)

		return is_same_line

	def value(self, node: ValueNode, level: int) -> None:
		start, end = node_range(node)

		if isinstance(node, CONTAINER_NODES):
			items = node.members if isinstance(node, ObjectNode) else node.elements

			if not items and self.peek_comment(end) is None:
				self.parts.append('{}' if isinstance(node, ObjectNode) else '[]')
				return

			if self.is_same_line(start, end):
				self.one_line(items, start, end, level)
				return

			self.parts.append(self.text[start])
			self.write_list(items, start + 1, end - 1, level + 1, is_bracketed=True)
			self.new_line(level, is_blank=False)
			self.parts.append(self.text[end - 1])
			return

		text = self.text[start:end]
		self.parts.append(reindent_block_string(text, INDENT * level) if isinstance(node, StringNode) and node.block else text)

	def print_document(self) -> str:
		body = self.body
		items = body.members if isinstance(body, ObjectNode) and not body.braced else (body,)
		# The document is a list without brackets: the members of a brace-less object, or one braced collection.
		self.write_list(items, 0, len(self.text), 0, is_bracketed=False)
		self.parts.append('\n')
		return ''.join(self.parts)


def reindent_block_string(text: str, indentation: str) -> str:
	"""Moves a block string to a new indentation. Its content is relative to the closing delimiter's indentation, so replacing that indentation on every line keeps the value. A blank line is left as it is, because it becomes an empty line either way."""
	# Note: Not using `textwrap.dedent()` because SOML uses the closing delimiter's indentation, not the common indentation of the content.
	closing_start = text.rfind('\n') + 1
	indentation_length = skip_spaces(text, closing_start) - closing_start
	if indentation_length == len(indentation) and text.startswith(indentation, closing_start):
		return text

	lines = text.split('\n')
	for index in range(1, len(lines)):
		line = lines[index]
		if line.strip(' \t'):
			lines[index] = indentation + line[indentation_length:]
	return '\n'.join(lines)
