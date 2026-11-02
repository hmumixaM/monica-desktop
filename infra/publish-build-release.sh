#!/usr/bin/env bash
# Alternate delivery for Actions-built images when Actions artifact quota is full.
# A draft in this private repository is accessible to the existing repo token.
set -euo pipefail
: "${BUILD_SHA:?Missing build SHA}" "${GH_REPO:?Missing repository}"
[[ "$BUILD_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$(gh repo view "$GH_REPO" --json isPrivate --jq .isPrivate)" == true ]]
tag="build-$BUILD_SHA"
if [[ "${BUILD_PREPARE_ONLY:-0}" == 1 ]] && ! gh release view "$tag" >/dev/null 2>&1; then
  # Only the preparation job creates the draft. GitHub permits multiple
  # unpublished drafts with the same tag, so matrix jobs must never create it.
  gh release create "$tag" --draft --target "$BUILD_SHA" --title "Build $BUILD_SHA" \
    --notes 'Private deployment images built by GitHub Actions. Not a product release.'
fi
[[ "$(gh release view "$tag" --json isDraft --jq .isDraft)" == true ]]
if [[ "${BUILD_PREPARE_ONLY:-0}" == 1 ]]; then exit 0; fi
: "${BUILD_SERVICE:?Missing service}"
[[ "$BUILD_SERVICE" =~ ^(api|web|worker)$ ]]
cd /tmp
asset="monica-next-$BUILD_SERVICE.tar"
sha256sum "$asset" > "$asset.sha256"
gh release upload "$tag" "$asset" "$asset.sha256" --clobber
