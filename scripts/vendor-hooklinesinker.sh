#!/usr/bin/env bash
# Stage pinned hooklinesinker release artifacts into ringleader/_vendor/ so one wheel
# works on the four supported architectures.
#
#   scripts/vendor-hooklinesinker.sh [dist-dir]
#
# `dist-dir` is a directory produced by hooklinesinker's scripts/build-release.sh (or an
# unpacked GitHub release): the three binaries plus the SHA256SUMS manifest they are
# verified against. Defaults to ../hooklinesinker/dist, or $HOOKLINESINKER_DIST.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist="${1:-${HOOKLINESINKER_DIST:-$repo_root/../hooklinesinker/dist}}"
vendor="$repo_root/ringleader/_vendor"

artifacts=(
    "macos-universal:hooklinesinker-macos-universal"
    "linux-aarch64:hooklinesinker-linux-aarch64"
    "linux-x86_64:hooklinesinker-linux-x86_64"
)

if command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$@"; }
else
    sha256() { sha256sum "$@"; }
fi

if [ ! -d "$dist" ]; then
    echo "no hooklinesinker dist directory at $dist" >&2
    echo "build one with: (cd <hooklinesinker> && scripts/build-release.sh)" >&2
    exit 1
fi

if [ ! -f "$dist/SHA256SUMS" ]; then
    echo "$dist/SHA256SUMS is missing — refusing to vendor unverified binaries" >&2
    exit 1
fi

echo "Verifying $dist/SHA256SUMS..."
for entry in "${artifacts[@]}"; do
    source_name="${entry#*:}"
    source_path="$dist/$source_name"
    if [ ! -f "$source_path" ]; then
        echo "$source_path is missing from $dist" >&2
        exit 1
    fi
    expected="$(awk -v name="$source_name" '
        {
            filename = $2
            sub(/^\*/, "", filename)
            sub(/^\.\//, "", filename)
            if (filename == name) print $1
        }
    ' "$dist/SHA256SUMS")"
    if [ -z "$expected" ]; then
        echo "$dist/SHA256SUMS has no checksum for $source_name" >&2
        exit 1
    fi
    actual="$(sha256 "$source_path" | cut -d' ' -f1)"
    if [ "$actual" != "$expected" ]; then
        echo "checksum mismatch for $source_name: expected $expected, got $actual" >&2
        exit 1
    fi
done

rm -rf "${vendor:?}"/*/
manifest="$vendor/checksums.txt"
: >"$manifest"

for entry in "${artifacts[@]}"; do
    platform="${entry%%:*}"
    source_name="${entry#*:}"
    source_path="$dist/$source_name"
    mkdir -p "$vendor/$platform"
    cp "$source_path" "$vendor/$platform/hooklinesinker"
    chmod +x "$vendor/$platform/hooklinesinker"
    digest="$(sha256 "$vendor/$platform/hooklinesinker" | cut -d' ' -f1)"
    echo "$digest  $platform/hooklinesinker" >>"$manifest"
    echo "  staged $platform/hooklinesinker"
done

echo "Wrote $manifest"
