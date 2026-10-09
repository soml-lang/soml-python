"""Regression tests for binary file writes."""

import io

import pytest

import soml


@pytest.mark.parametrize('written', [0, 11, 14, None])
def test_dump_reports_an_incomplete_binary_write(written: int | None) -> None:
	output = bytearray()

	class IncompleteWriter:
		def write(self, data: bytes, /) -> int | None:
			assert data == "message: 'é😀'\n".encode()
			if written is not None:
				output.extend(data[:written])
			return written

	with pytest.raises(OSError, match='incomplete write'):
		soml.dump({'message': 'é😀'}, IncompleteWriter())  # type: ignore[arg-type] # A nonblocking raw writer may return None, which dump must also reject at runtime.

	assert output == "message: 'é😀'\n".encode()[: written or 0]


def test_dump_propagates_the_original_stream_error() -> None:
	error = OSError('disk full')

	class FailedWriter:
		def write(self, data: bytes, /) -> int:
			assert data == b'a: 1\n'
			raise error

	with pytest.raises(OSError, match='disk full') as caught:
		soml.dump({'a': 1}, FailedWriter())

	assert caught.value is error


def test_dump_does_not_close_or_flush_a_stream_owned_by_the_caller() -> None:
	stream = io.BytesIO()
	soml.dump({'a': 1}, stream)
	soml.dump({'b': 2}, stream)

	assert not stream.closed
	assert stream.getvalue() == b'a: 1\nb: 2\n'
