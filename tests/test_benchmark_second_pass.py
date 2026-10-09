"""Benchmark inputs and metadata remain suitable for comparing saved runs."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip('orjson')
pytest.importorskip('tomli_w')
pytest.importorskip('tomlkit')

spec = importlib.util.spec_from_file_location('benchmark', Path(__file__).parents[1] / 'bench' / 'benchmark.py')
assert spec is not None
assert spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

if TYPE_CHECKING:
	from collections.abc import Callable


def test_fixture_loading_preserves_utf8_and_every_line_ending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	text = '[\r\n\t"é😀",\r\n\t"line\\r\\n", "\u2028\u0085"\r\n]\r\n'
	(tmp_path / 'service.json').write_bytes(text.encode())
	seen: list[str] = []

	def read(source: str) -> object:
		seen.append(source)
		return json.loads(source)

	def measure(run: Callable[[], object], *, seconds: float, repeat: int) -> float:
		assert seconds > 0
		assert repeat > 0
		assert run() == ['é😀', 'line\r\n', '\u2028\u0085']
		return 0.001

	monkeypatch.setattr(benchmark, 'FIXTURES', tmp_path)
	monkeypatch.setattr(benchmark, 'LIBRARIES', {'json': ('json', read, json.dumps)})
	monkeypatch.setattr(benchmark, 'measure', measure)
	monkeypatch.setattr(sys, 'argv', ['benchmark.py', '--fixtures', 'service', '--libraries', 'json', '--operations', 'read', '--json'])
	benchmark.main()
	assert seen == [text]
	assert json.loads(capsys.readouterr().out)['timings'] == {'Read': {'service': {'json': 0.001}}}


def test_json_records_only_the_versions_of_selected_libraries(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
	monkeypatch.setattr(benchmark, 'measure', lambda *_arguments, **_options: 0.001)
	monkeypatch.setattr(benchmark, 'version', lambda package: f'{package}-version')
	monkeypatch.setattr(sys, 'argv', ['benchmark.py', '--fixtures', 'service', '--libraries', 'json', 'soml', 'tomllib / tomli-w', '--operations', 'read', '--json'])
	benchmark.main()
	environment = json.loads(capsys.readouterr().out)['environment']
	assert environment['libraries'] == {'json': {}, 'soml': {'soml-lang': 'soml-lang-version'}, 'tomllib / tomli-w': {'tomli-w': 'tomli-w-version'}}
	assert list(environment['libraries']) == ['json', 'soml', 'tomllib / tomli-w']
	assert environment['python']


@pytest.mark.parametrize('seconds', [0.0, -0.001, float('nan'), float('inf'), -float('inf')])
def test_measure_rejects_invalid_sampling_time_before_running_work(seconds: float) -> None:
	def run() -> None:
		raise AssertionError('Invalid measurements must not run the operation')

	with pytest.raises(ValueError, match='seconds must be finite and greater than zero'):
		benchmark.measure(run, seconds=seconds, repeat=1)


@pytest.mark.parametrize('repeat', [0, -1])
def test_measure_rejects_invalid_batch_counts_before_running_work(repeat: int) -> None:
	def run() -> None:
		raise AssertionError('Invalid measurements must not run the operation')

	with pytest.raises(ValueError, match='repeat must be greater than zero'):
		benchmark.measure(run, seconds=0.001, repeat=repeat)
