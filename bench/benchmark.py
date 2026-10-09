"""
Reads and writes the shared benchmark fixtures with soml and the most used JSON and TOML libraries, and prints the time per document as the Markdown table in the readme.

	uv run --group bench python bench/benchmark.py

Each time is the least CPU time of several runs, so that other processes on the machine count as little as possible.
"""

import argparse
import json
import math
import platform
import time
import timeit
import tomllib
from collections.abc import Callable
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any

import orjson
import tomli_w
import tomlkit

import soml

# A copy of the `benchmark` directory of the `soml` repository. Each fixture has the same data in each format, with durations and instants as ISO 8601 strings in JSON and TOML.
FIXTURES = Path(__file__).parent / 'fixtures'

NAMES = ('service', 'platform', 'earthquakes')

# Each column reads, and writes, its format. A writer gets the value that the most used reader of its format returns.
# Indented, as a person writes a config file, and as `soml.dumps()` writes. orjson indents with two spaces only.
LIBRARIES: dict[str, tuple[str, Callable[[str], Any], Callable[[Any], object]]] = {
	'soml': ('soml', soml.loads, soml.dumps),
	'json': ('json', json.loads, partial(json.dumps, indent='\t')),
	'orjson': ('json', orjson.loads, partial(orjson.dumps, option=orjson.OPT_INDENT_2)),
	'tomllib / tomli-w': ('toml', tomllib.loads, tomli_w.dumps),
	'tomlkit': ('toml', tomlkit.loads, tomlkit.dumps),
}

STANDARD_READERS: dict[str, Callable[[str], Any]] = {
	'soml': soml.loads,
	'json': json.loads,
	'toml': tomllib.loads,
}


def measure(run: Callable[[], object], *, seconds: float, repeat: int) -> float:
	"""The least CPU time per call across calibrated batches. `timeit` restores the caller's garbage-collector state."""
	if not math.isfinite(seconds) or seconds <= 0:
		raise ValueError('seconds must be finite and greater than zero')
	if repeat <= 0:
		raise ValueError('repeat must be greater than zero')

	timer = timeit.Timer(run, timer=time.process_time)
	# Note: Not using `Timer.autorange()` because its fixed 0.2-second calibration target ignores the configurable batch duration.
	count = 1
	while (elapsed := timer.timeit(count)) < min(seconds, 0.01):
		count *= 10

	count = max(1, round(count * seconds / elapsed))
	return min(timer.repeat(repeat=repeat, number=count)) / count


def positive_seconds(text: str) -> float:
	"""A finite, positive sampling time for the command-line interface."""
	value = float(text)
	if not math.isfinite(value) or value <= 0:
		raise argparse.ArgumentTypeError('seconds must be finite and greater than zero')
	return value


def positive_count(text: str) -> int:
	"""A positive number of measurement batches."""
	value = int(text)
	if value <= 0:
		raise argparse.ArgumentTypeError('repeat must be greater than zero')
	return value


def format_time(seconds: float) -> str:
	if seconds < 0.001:
		return f'{seconds * 1_000_000:.0f} µs'

	if seconds < 0.01:
		return f'{seconds * 1000:.1f} ms'

	return f'{seconds * 1000:,.0f} ms'


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, suggest_on_error=True)
	parser.add_argument('--fixtures', nargs='+', choices=NAMES, default=NAMES, help='fixtures to measure')
	parser.add_argument('--seconds', type=positive_seconds, default=0.2, help='CPU seconds per measurement batch (default: 0.2)')
	parser.add_argument('--repeat', type=positive_count, default=5, help='number of batches (default: 5)')
	parser.add_argument('--libraries', nargs='+', choices=tuple(LIBRARIES), default=tuple(LIBRARIES), help='libraries to measure')
	parser.add_argument('--operations', nargs='+', choices=('read', 'write'), default=('read', 'write'), help='operations to measure')
	parser.add_argument('--json', action='store_true', help='write raw timings in seconds as JSON')
	options = parser.parse_args()
	libraries = {name: LIBRARIES[name] for name in options.libraries}
	fixtures = tuple(dict.fromkeys(options.fixtures))
	extensions = {extension for extension, _, _ in libraries.values()}
	texts = {(name, extension): (FIXTURES / f'{name}.{extension}').read_text(encoding='utf-8', newline='') for name in fixtures for extension in extensions}
	values = {key: STANDARD_READERS[key[1]](text) for key, text in texts.items()} if 'write' in options.operations else {}
	results: dict[str, dict[str, dict[str, float]]] = {}

	if not options.json:
		print(f'| | {" | ".join(libraries)} |')
		print(f'|---|{"---:|" * len(libraries)}')

	for heading, is_reading, arguments in (('Read', True, texts), ('Write', False, values)):
		if heading.lower() not in options.operations:
			continue

		results[heading] = {}
		if not options.json:
			print(f'| **{heading}** |{" |" * len(libraries)}')

		for name in fixtures:
			timings = {
				library: measure(partial(reader if is_reading else writer, arguments[name, extension]), seconds=options.seconds, repeat=options.repeat)
				for library, (extension, reader, writer) in libraries.items()
			}
			results[heading][name] = timings
			if not options.json:
				print(f'| {name} | {" | ".join(map(format_time, timings.values()))} |', flush=True)

	if options.json:
		packages = {'soml': ('soml-lang',), 'json': (), 'orjson': ('orjson',), 'tomllib / tomli-w': ('tomli-w',), 'tomlkit': ('tomlkit',)}
		metadata = {
			'python': platform.python_version(),
			'platform': platform.platform(),
			'architecture': platform.machine(),
			'timer': 'process_time',
			'seconds_per_batch': options.seconds,
			'batches': options.repeat,
			'libraries': {name: {package: version(package) for package in packages[name]} for name in libraries},
		}
		print(json.dumps({'environment': metadata, 'timings': results}, indent='\t', allow_nan=False))


if __name__ == '__main__':
	main()
