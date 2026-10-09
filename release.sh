#!/usr/bin/env bash
# Releases a new version through the Release workflow.
# Usage: ./release.sh <version> (for example `./release.sh 1.2.3` or `./release.sh v1.2.3`)

set -euo pipefail

cd "$(dirname "$0")"

# Checks that the first version is greater than the second, comparing the numeric `x.y.z` triple. A release is always newer than a prerelease of the same version. Versions are in PEP 440, as `uv version` writes them: `1.2.3-alpha.1` becomes `1.2.3a1`.
version_newer() {
	local left right
	# Strip a semver prerelease after a hyphen, or a PEP 440 prerelease or postrelease segment, so `1.2.3-rc.1`, `1.2.3a1`, `1.2.3rc1`, and `1.2.3.dev1` all give `1.2.3`.
	left=${1%%[ab]*}
	left=${left%%-*}
	left=${left%%rc*}
	left=${left%%.dev*}
	left=${left%%.post*}
	right=${2%%[ab]*}
	right=${right%%-*}
	right=${right%%rc*}
	right=${right%%.dev*}
	right=${right%%.post*}

	local IFS=.
	local -a lparts
	local -a rparts
	read -r -a lparts <<< "$left"
	read -r -a rparts <<< "$right"
	for i in 0 1 2; do
		if ((lparts[i] > rparts[i])); then
			return 0
		fi
		if ((lparts[i] < rparts[i])); then
			return 1
		fi
	done
	# A release beats a prerelease of the same version, as with `1.2.3` over `1.2.3a1`, but not a postrelease, which a release is below in PEP 440 (`1.2.3 < 1.2.3.post1`).
	if [[ $1 == "$left" && $2 != "$right" && $2 != "$right".post* ]]; then
		return 0
	fi
	return 1
}

if [[ $# -ne 1 ]]; then
	echo "Usage: $(basename "$0") <version>" >&2
	exit 1
fi

version=${1#v}
if [[ ! $version =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
	echo "Invalid version: $1" >&2
	exit 1
fi

# `uv version` writes a prerelease in PEP 440, so `1.2.3-alpha.1` becomes `1.2.3a1`. The workflow tags what `uv version` wrote, so use the written form everywhere below.
version=$(uv version --dry-run --output-format json "$version" | jq -r .version)

# A postrelease, as `1.2.3-post.1` becomes `1.2.3.post1`, is not supported: it is not a prerelease, and a release is below it.
if [[ $version == *.post* ]]; then
	echo "Post releases are not supported: $1" >&2
	exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
	echo "The working tree is not clean." >&2
	exit 1
fi

branch=$(git branch --show-current)
if [[ $branch != main ]]; then
	echo "Releases are made from main, not $branch." >&2
	exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
	echo "gh (GitHub CLI) is not installed." >&2
	exit 1
fi

if ! gh auth status --active >/dev/null 2>&1; then
	echo "gh is not authenticated. Run: gh auth login" >&2
	exit 1
fi

git fetch origin main --tags
if [[ $(git rev-parse HEAD) != $(git rev-parse origin/main) ]]; then
	echo "main is not in sync with origin/main." >&2
	exit 1
fi

if git rev-parse --verify --quiet "refs/tags/v$version" >/dev/null; then
	echo "The tag v$version already exists." >&2
	exit 1
fi

current_version=$(uv version --short)
if [[ $current_version == "$version" ]]; then
	echo "pyproject.toml is already at $version." >&2
	exit 1
fi
if ! version_newer "$version" "$current_version"; then
	echo "The new version must be greater than $current_version." >&2
	exit 1
fi

echo "Releasing $current_version -> $version"

# Run the checks before touching anything, so a failure does not leave a dirty working tree.
# The same checks as the Release workflow, which publishes the version in pyproject.toml.
# `--all-groups` because mypy also checks `bench`.
uv sync --locked --all-groups
uv run --no-sync pytest
uv run --no-sync ruff check
uv run --no-sync ruff format --check
uv run --no-sync mypy
uv run --no-sync pyright

uv version "$version"

if [[ $(uv version --short) != "$version" ]]; then
	echo "Failed to update the version in pyproject.toml." >&2
	exit 1
fi

# Validate the new version as the Release workflow does: the lock must stay consistent with `--locked`, and the package must build.
if ! { uv sync --locked --all-groups && uv build; }; then
	echo "The checks failed after the bump. Revert it with: git restore pyproject.toml uv.lock" >&2
	exit 1
fi

git add pyproject.toml uv.lock
git commit -m "$version"
if ! git push origin main; then
	echo "The version bump is committed but the push failed. Retry with: git push origin main" >&2
	exit 1
fi

head_sha=$(git rev-parse HEAD)

if ! gh workflow run release.yml --ref main; then
	echo "The version bump is already pushed. Start the Release workflow from the Actions tab, or with: gh workflow run release.yml --ref main" >&2
	exit 1
fi

run_id=
for ((i = 0; i < 60; i++)); do
	if ! run_id=$(gh run list --workflow release.yml --branch main --limit 10 --json databaseId,headSha --jq ".[] | select(.headSha == \"$head_sha\") | .databaseId" | head -n 1); then
		echo "Failed to query the release run." >&2
		exit 1
	fi
	if [[ -n $run_id ]]; then
		break
	fi
	sleep 2
done

if [[ -z $run_id ]]; then
	echo "Could not find the release run for this commit. The version bump is already pushed and the workflow may still publish it. Track it with: gh run list --workflow release.yml" >&2
	exit 1
fi

if ! gh run watch "$run_id" --exit-status; then
	echo "The Release workflow failed. Check the Actions tab. If the package was published before the failure, it cannot be re-run; create the missing resources with: gh release create v$version --generate-notes" >&2
	exit 1
fi

if ! git fetch origin main --tags; then
	echo "Could not fetch the tag. The package may be published and the tag pushed. Check the Actions tab." >&2
	exit 1
fi
if ! tag_sha=$(git rev-parse --verify --quiet "refs/tags/v$version"); then
	tag_sha=
fi
if [[ -z $tag_sha ]]; then
	echo "The tag v$version was not pushed. The package may not be published. Check the Actions tab." >&2
	exit 1
fi
if [[ $tag_sha != "$head_sha" ]]; then
	echo "The tag v$version does not point at the released commit." >&2
	exit 1
fi

if ! gh release create "v$version" --generate-notes; then
	echo "The package is published and the tag is pushed, but creating the GitHub release failed. Retry with: gh release create v$version --generate-notes" >&2
	exit 1
fi

echo "Released v$version" >>"${TMPDIR:-/tmp}/soml-release.log" 2>&1
