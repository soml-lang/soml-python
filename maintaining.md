# Maintaining

## Publish

The package is `soml-lang` on PyPI, because PyPI rejects `soml` as too similar to the existing `somi` project. The import name is still `soml`.

The [Release workflow](.github/workflows/release.yml) publishes it with [trusted publishing](https://docs.pypi.org/trusted-publishers/), so there is no API token to keep. Set this up once on PyPI: in the project's publishing settings, add a GitHub publisher with the owner `soml-lang`, the repository `soml-python`, and the workflow `release.yml`.

1. Run `./release.sh <version>`, for example `./release.sh 1.2.3` or `./release.sh v1.2.3`. It bumps the version in `pyproject.toml`, runs the same checks as the Release workflow, commits and pushes, runs the workflow (which tests, publishes the package, and pushes the `v1.2.3` tag), and creates the release on GitHub.
