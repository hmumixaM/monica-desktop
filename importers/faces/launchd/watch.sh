#!/bin/sh
# Kept running by launchd. Credentials live in a 600 file outside the repository.
set -eu
. "$HOME/.config/monica-faces/env"
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
cd "$(dirname "$0")/.."
exec nice -n 15 "$HOME/.local/bin/uv" run --frozen monica-faces watch
