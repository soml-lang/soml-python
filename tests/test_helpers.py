"""The timing helper keeps measurements and caller state predictable."""

import gc
from types import SimpleNamespace

import helpers
import pytest


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_best_time_preserves_collection_state_even_when_the_workload_raises(*, enabled: bool, raises: bool) -> None:
	was_enabled = gc.isenabled()
	if enabled:
		gc.enable()
	else:
		gc.disable()

	def run() -> None:
		assert not gc.isenabled()
		if raises:
			raise RuntimeError('failed workload')

	try:
		if raises:
			with pytest.raises(RuntimeError, match='failed workload'):
				helpers.best_time(run, runs=1)
		else:
			assert helpers.best_time(run, runs=1) >= 0
		assert gc.isenabled() is enabled
	finally:
		if was_enabled:
			gc.enable()
		else:
			gc.disable()


def test_best_time_returns_the_least_cpu_time_for_the_requested_number_of_calls(monkeypatch: pytest.MonkeyPatch) -> None:
	ticks = iter((0, 7, 10, 15, 20, 21))
	monkeypatch.setattr(helpers, 'time', SimpleNamespace(process_time=lambda: next(ticks)))
	calls = 0

	def run() -> None:
		nonlocal calls
		calls += 1

	assert helpers.best_time(run, runs=3) == 1
	assert calls == 3
