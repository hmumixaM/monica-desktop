#!/bin/sh
# Backwards-compatible entry point. Candidate containers are verified before
# proxy reload; earlier containers remain available for rollback.
set -eu
exec "${PYTHON_BINARY:-python3}" "$(dirname "$0")/deploy-staged-images.py" "$@"
