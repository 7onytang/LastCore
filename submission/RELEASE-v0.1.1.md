# LastCore v0.1.1

Publication date: 2026-10-13, Asia/Shanghai. This is a public, non-prerelease GitHub distribution, not a claim of complete production/platform acceptance.

This record below preserves the initial publication scope at source `bf5b82e`. Subsequent authorized deployment, preview attachments and source supplements are recorded separately; this documentation update does not replace the original tag or archives.

## Current supplements (2026-10-13, Asia/Shanghai)

- The [deployment receipt](evidence/public-deployment-current.md) records formal Controller source `bf5b82e` at 18:41:15 Beijing; `cc7c7ba` console screenshots remain historical captures.
- [Windows/Linux portable previews](../edge-client/README.md) are published at component version `0.2.0-preview`, build source `85a1e232`. Native compilation and static checks passed; GUI, network, device and relay operation remain unverified. They are unsigned and support bounded linear calculations only.
- The [Android development preview](../README.md) is attached as app version `0.1.0-draft` with Android Debug signing; physical-device acceptance remains deferred.
- [Complete source ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCore-v0.1.1-complete-source.zip), source `6977c0919c9df3323cb0ec52e2b8820646df8233`, is separate from initial core `bf5b82e` and immutable r2 material source `ac5d630`. [Checksum](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCore-v0.1.1-complete-source.zip.sha256) and [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/COMPLETE_SOURCE_MANIFEST.json) accompany it. No new runtime, model or device acceptance is claimed.

## Initial publication changes

- PR #67: shared Controller console, Android UI and source-only Python node panel with bounded own-task observations and local process controls.
- PR #70: POSIX/Windows state locking using Python's existing fcntl/msvcrt implementations.
- PR #72: tiny batch limit of 64, checked before stage dtype conversion.
- PR #73: curated historical Mac client linear acceptance summary, with its original evidence scope.
- PR #75: prepared console-entry promotion operator. It has not been executed by this publication.
- Release integration: clear the console credential on a 401 from the optional whole-model worker endpoint; add Linux/Windows credential-lock CI jobs.

The mainline review-thread API returned no inline threads for #67, #70, #71 and #72. Review covered source changes, API/credential boundaries and existing CI metadata. The source PRs #67, #70 and #72 had successful existing checks; these are historical branch checks, not fresh combined-release validation. Windows execution had not been proven by those existing checks.

## Initial publication verification and pending work

The user explicitly requested immediate publication without further real execution. No new real inference task, device test, deployment or combined pytest run was completed for this release. Diff whitespace and JavaScript syntax checks passed before the final documentation update. New CI runs may complete after publication and must be read independently.

At initial publication, PR #32 and #71 were excluded because their then-current branches conflicted with main and their Controller test checks failed. #32's native client/targeted-linear work is already present in main through the scoped #68 integration; its remaining broad Controller/demo changes are not imported wholesale. #71 additionally introduces new workload/result contracts and computation loops with no cancellation checks during the actual pi/log work. These require a compatible, focused follow-up. At initial publication, PR #74 duplicated the two-file lock change from #70; native Windows/Linux execution packaging remained in progress and was not released. PR #74 was subsequently independently reviewed and merged for the separately source-bound previews above. Historical stacked/refactor/prototype PRs remain outside this release.

## Initial publication artifacts and operation

The release source archive is exported from the exact release commit with git archive and accompanied by SHA256 and a manifest. It contains tracked source only, excluding local credentials, database state, build/cache directories and the untracked submission-output directory.

The optional Mac DMG is copied unchanged from v0.1.1-alpha.1, source f91397620575444e253c528a71105b09c2ef776d. Its original provenance and checksum remain attached. The macos-client source is unchanged in this release. It is Apple Silicon/macOS 13+, ad-hoc signed and not notarized. At initial publication, Android physical-device acceptance, Windows/Linux native packages and production client signing were pending. The later unsigned Windows/Linux previews above do not establish device acceptance or production signing.

Controller: follow the root README local/Docker instructions. Source node panel: follow node-agent/desktop/README.md and install requirements-ui.txt in a private environment. Native Mac client: follow macos-client/README.md. No public server, credentials or production database were changed by publication; existing immutable a6 materials remain unchanged.

Implementation references: [CPython msvcrt locking](https://github.com/python/cpython/blob/main/Doc/library/msvcrt.rst) and [FastAPI StaticFiles](https://github.com/fastapi/fastapi/blob/master/docs/en/docs/tutorial/static-files.md). Existing maintained native dependencies are reused; no additional release framework is needed.
