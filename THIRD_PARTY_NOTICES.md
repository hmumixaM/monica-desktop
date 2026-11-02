# Third-party components

This repository preserves the notices and licenses that accompany copied components. Building the application also obtains pinned external dependencies; see the lockfiles and runtime manifest for their versions.

- **Chatlog Alpha**: MIT; public upstream <https://github.com/waawei/chatlog_alpha>. The source pin and local patch are in `apps/desktop/patches/chatlog/`, with the upstream license retained there.
- **LinkedIn enrichment adaptation**: GPL-3.0-only; see `apps/desktop/collectors/vendor/linkedin_enrichment/LICENSE` and `NOTICE`. Its pinned dependencies are recorded in the collector lockfile and `VENDORED_SOURCES.json`.
- **linkedin_scraper**: fetched from its pinned upstream commit; see <https://github.com/joeyism/linkedin_scraper>.
- **linkedin-cli**: fetched from its pinned upstream commit; MIT; see <https://github.com/darwincr/linkedin-cli>.
- **D3, TopoJSON client, and world-atlas**: notices are retained under `apps/web/public/vendor/`.
- **Instagram collector sources**: provenance and adapted file digests are recorded in `apps/desktop/collectors/VENDORED_SOURCES.json`.

InsightFace model weights, platform sessions, compiled runtimes, and personal archives are not vendored in this source repository. The builder supplies model files separately. This notice does not replace individual component licenses or add a new blanket license.
