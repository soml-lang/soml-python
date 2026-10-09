import os

from hypothesis import HealthCheck, settings

# The default profile keeps the whole suite at a few seconds. Run `HYPOTHESIS_PROFILE=thorough uv run pytest` for a long search.
settings.register_profile('default', max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile('thorough', max_examples=10_000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE', 'default'))
