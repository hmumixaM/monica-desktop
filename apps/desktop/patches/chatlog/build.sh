#!/bin/sh
set -eu

# Build a pinned, patched source tree without modifying an existing checkout.
# Optional CHATLOG_SOURCE_REPO can name a local clone to avoid downloading Git.
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/chatlog-alpha" >&2
  exit 2
fi
case "$1" in
  /*) output=$1 ;;
  *) echo 'The output path must be absolute.' >&2; exit 2 ;;
esac
patch_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
build_root=$(mktemp -d "${TMPDIR:-/tmp}/monica-chatlog-build.XXXXXX")
trap 'rm -rf "$build_root"' EXIT HUP INT TERM
source_repo=${CHATLOG_SOURCE_REPO:-https://github.com/waawei/chatlog_alpha.git}
git clone --quiet --no-checkout "$source_repo" "$build_root/source"
git -C "$build_root/source" checkout --quiet --detach 53ab86b7fe3c01a08b312d34d7b3d866e34a721e
git -C "$build_root/source" apply --check "$patch_dir/desktop.patch"
git -C "$build_root/source" apply "$patch_dir/desktop.patch"
mkdir -p "$(dirname -- "$output")"
cd "$build_root/source"
CGO_ENABLED=1 GOOS=darwin GOARCH=arm64 go build -trimpath -o "$output" .
