"""Checks for the benchmark's units, sampling, and CLI validation."""

import argparse
import gc
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip('orjson')
pytest.importorskip('tomli_w')
pytest.importorskip('tomlkit')

spec = importlib.util.spec_from_file_location('benchmark', Path(__file__).parents[1] / 'bench' / 'benchmark.py')
assert spec is not None
assert spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


@pytest.mark.parametrize('enabled', [False, True])
def test_measure_runs_real_work_and_preserves_the_callers_gc_setting(*, enabled: bool) -> None:
	count = 0

	def run() -> None:
		nonlocal count
		count += 1

	was_enabled = gc.isenabled()
	if enabled:
		gc.enable()
	else:
		gc.disable()
	try:
		seconds = benchmark.measure(run, seconds=0.001, repeat=2)
		assert seconds > 0
		assert count > 200
		assert gc.isenabled() is enabled
	finally:
		if was_enabled:
			gc.enable()
		else:
			gc.disable()


@pytest.mark.parametrize('text', ['nan', 'inf', '-inf', '0', '-1'])
def test_invalid_sampling_times_are_rejected(text: str) -> None:
	with pytest.raises(argparse.ArgumentTypeError, match='finite and greater than zero'):
		benchmark.positive_seconds(text)


@pytest.mark.parametrize('text', ['0', '-1'])
def test_invalid_batch_counts_are_rejected(text: str) -> None:
	with pytest.raises(argparse.ArgumentTypeError, match='greater than zero'):
		benchmark.positive_count(text)
