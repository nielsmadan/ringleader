#!/usr/bin/env bash
# Stage one bundled tool's release artifacts into ringleader/_vendor/ so one wheel works on
# the four supported architectures.
#
#   scripts/vendor-binary.sh <hooklinesinker|slimetrail> [dist-dir]
#
# `dist-dir` holds the tool's three release binaries plus the SHA256SUMS manifest they are
# verified against: a GitHub release or the tool's own release build. Defaults to
# $<TOOL>_DIST, else ../<tool>/dist. Only this tool's binaries and checksum lines are
# replaced; the other tool's stay as they are.

set -euo pipefail

tool="${1:-}"
case "$tool" in
hooklinesinker | slimetrail) ;;
*)
    echo "usage: $0 <hooklinesinker|slimetrail> [dist-dir]" >&2
    exit 2
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist_variable="$(tr '[:lower:]' '[:upper:]' <<<"$tool")_DIST"
dist="${2:-${!dist_variable:-$repo_root/../$tool/dist}}"
vendor="$repo_root/ringleader/_vendor"
platforms=(macos-universal linux-aarch64 linux-x86_64)

if command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$@"; }
else
    sha256() { sha256sum "$@"; }
fi

if [ ! -d "$dist" ]; then
    echo "no $tool dist directory at $dist" >&2
    exit 1
fi

if [ ! -f "$dist/SHA256SUMS" ]; then
    echo "$dist/SHA256SUMS is missing — refusing to vendor unverified binaries" >&2
    exit 1
fi

echo "Verifying $dist/SHA256SUMS..."
for platform in "${platforms[@]}"; do
    source_name="$tool-$platform"
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

mkdir -p "$vendor"
manifest="$vendor/checksums.txt"
pending="$(mktemp "$vendor/.checksums.XXXXXX")"
trap 'rm -f "$pending"' EXIT
if [ -f "$manifest" ]; then
    awk -v tool="$tool" '
        NF {
            filename = $2
            sub(/^\*/, "", filename)
            n = split(filename, parts, "/")
            if (parts[n] != tool) print
        }
    ' "$manifest" >"$pending"
fi

for platform in "${platforms[@]}"; do
    staged="$vendor/$platform/$tool"
    mkdir -p "$vendor/$platform"
    cp "$dist/$tool-$platform" "$staged"
    chmod +x "$staged"
    digest="$(sha256 "$staged" | cut -d' ' -f1)"
    echo "$digest  $platform/$tool" >>"$pending"
    echo "  staged $platform/$tool"
done

chmod 644 "$pending"
mv "$pending" "$manifest"
echo "Wrote $manifest"
