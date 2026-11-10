#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=$(<"$repo_root/tools/k6-version")
archive_sha=$(awk 'NR==1 {print $1}' "$repo_root/tools/k6-version.sha256")
binary_sha=$(awk 'NR==1 {print $1}' "$repo_root/tools/k6-binary-linux-amd64.sha256")
destination=${1:-"$repo_root/.tools/k6-$version"}

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  printf 'pinned k6 installer supports Linux x86_64 only (found %s %s)\n' "$(uname -s)" "$(uname -m)" >&2
  exit 2
fi

mkdir -p "$destination"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
archive="$tmp/k6-$version-linux-amd64.tar.gz"
url="https://github.com/grafana/k6/releases/download/$version/k6-$version-linux-amd64.tar.gz"
curl --fail --location --silent --show-error "$url" --output "$archive"
printf '%s  %s\n' "$archive_sha" "$archive" | sha256sum --check --status || {
  printf 'official k6 release archive checksum does not match the repository pin\n' >&2
  exit 1
}
tar --extract --gzip --file "$archive" --directory "$tmp"
binary="$tmp/k6-$version-linux-amd64/k6"
printf '%s  %s\n' "$binary_sha" "$binary" | sha256sum --check --status || {
  printf 'k6 binary checksum does not match the repository pin\n' >&2
  exit 1
}
install -m 0755 "$binary" "$destination/k6"
"$destination/k6" version
printf 'installed pinned k6 at %s\n' "$destination/k6"
