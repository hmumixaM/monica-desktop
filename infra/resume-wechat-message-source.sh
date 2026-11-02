#!/bin/sh
set -eu

DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
COMPOSE=/var/packages/ContainerManager/target/usr/bin/docker-compose
INFRA=/path/to/nas/home/docker/monica-next/infra
OLD=${1:-monica-wechat-source-copy-9a68458}
NEW=${2:-monica-wechat-source-copy-complete}
REQUESTED_RELEASE=${3:-}
BATCH_SIZE=${BATCH_SIZE:-5000}

if "$DOCKER" inspect "$NEW" >/dev/null 2>&1; then
    state=$("$DOCKER" inspect --format '{{.State.Status}}' "$NEW")
    [ "$state" = running ] && exit 0
    "$DOCKER" start "$NEW" >/dev/null
    exit 0
fi

if "$DOCKER" inspect "$OLD" >/dev/null 2>&1; then
    state=$("$DOCKER" inspect --format '{{.State.Status}}' "$OLD")
    if [ "$state" = running ]; then
        "$DOCKER" stop --time 600 "$OLD" >/dev/null
    fi
    exit_code=$("$DOCKER" inspect --format '{{.State.ExitCode}}' "$OLD")
    [ "$exit_code" = 0 ] || [ "$exit_code" = 75 ]
fi

release=${REQUESTED_RELEASE:-$(python3 -c 'import json;print(json.load(open("'"$INFRA"'/.release-state.json"))["release"])')}
cd "$INFRA"
IMAGE_TAG=$release "$COMPOSE" run -d --no-deps -T --name "$NEW" api \
    /app/.venv/bin/python -u -m app.platform_migrate messages \
    --provider wechat --batch-size "$BATCH_SIZE" --batch-pause 0.05 >/dev/null

"$DOCKER" inspect --format '{{.Name}} {{.State.Status}} {{.Config.Image}} {{json .Config.Cmd}}' "$NEW"
