"""Benchmark CLI regression tests using deterministic measurements."""

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


@pytest.mark.parametrize('operation', ['read', 'write'])
@pytest.mark.parametrize('fixtures', [('service', 'service'), ('platform', 'service', 'platform')])
def test_repeated_fixture_arguments_are_measured_once(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str, fixtures: tuple[str, ...]) -> None:
	calls = 0

	def measure(run: Callable[[], object], *, seconds: float, repeat: int) -> float:
		nonlocal calls
		assert seconds == 0.001
		assert repeat == 1
		assert run()
		calls += 1
		return 0.001

	monkeypatch.setattr(benchmark, 'measure', measure)
	monkeypatch.setattr(sys, 'argv', ['benchmark.py', '--fixtures', *fixtures, '--libraries', 'soml', '--operations', operation, '--seconds', '0.001', '--repeat', '1', '--json'])

	benchmark.main()

	selected = dict.fromkeys(fixtures)
	assert calls == len(selected)
	timings = json.loads(capsys.readouterr().out)['timings']
	assert timings == {operation.title(): {fixture: {'soml': 0.001} for fixture in selected}}
	assert list(timings[operation.title()]) == list(selected)
