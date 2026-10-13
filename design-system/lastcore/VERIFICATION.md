# Unified UI verification

Final UI source: `1760373d17489eb377f74635b079efd77e766bf0`, branch `codex/unified-ui`, draft PR [#67](https://github.com/7onytang/LastCore/pull/67). Any subsequent commit changes only verification documentation.

## Functional workflow
- **Web:** neutral charcoal workspace with a compact heading; connect -> credential input collapses -> refresh/disconnect. Invalid credentials clear memory/data and restore the form. Page navigation clears the authenticated UI as well as the token. Inputs/actions disable during requests, and stale results are labeled after failure.
- **Desktop:** idle shows start; running shows stop. A pending stop is not complete; new start stays unavailable until the original child exits. Failed/unknown requests permit a state recheck instead of blind reexecution. An expired page session keeps controls disabled with a reopen-link instruction.
- **Android:** shared model preparation, mutually exclusive local/cloud modes, expandable first enrollment, inline field errors and one primary operation. Model presence is a hint; exact hash checks still run. Missing model files do not prevent cloud lease recovery. Busy work disables competing actions and takes the user to output/stop; stopping remains pending until the existing executor completes.
- **Website:** original light showcase and evidence unchanged; short primary labels, visible reset text and >=48px interactive targets.

## Verified results
- **19 Controller regressions passed** with real temporary SQLite: public assets, private data authentication, traversal rejection and unchanged health behavior.
- **29 desktop/config/node regressions passed**, including loopback session/Host/Origin protection and a controlled delayed-child fixture proving stop timeout cannot report completion or launch another child. The delayed fixture is a simulated timing condition.
- **Real local browser/HTTP demo-node loop:** an actual configured demo-mode process registered and heartbeated to an isolated real Controller, then stopped with exit 0 through the same panel. This proves process/protocol controls, not model inference or production behavior.
- **Actual browser QA:** live local API data, input collapse/clearing, disconnect, invalid credential retry, start/stop visibility, expired-session disabled state, website replay/reset, and no horizontal overflow at 1440/768/390px.
- **Android full build-apk.sh passed:** debug, unsigned release and instrumentation APKs, lintDebug, fixed-source/license/artifact binding, 7 admission checks and 9 pure-Java HTTPS checks. Source binding is `1760373d17489eb377f74635b079efd77e766bf0`. Instrumentation was compiled, **not executed**.

## Android artifacts
- Debug: `16a08e7b407232c34d82cf443d0522c5946291bc8f15b8f31fdbf181c55a70a8` (development signature).
- Unsigned release: `4570f8394f3581693c4354885b101c027f664a20e1850e0632e13875fe9259eb`.
- Instrumentation: `ca198d00232fc5e15bc41825c1c5b64e0cc49a0a07e8ea498932fc43bcab4a71` (not executed).

Full build log: `/private/tmp/lastcore-ui-workflow-android-build.log`. Full local manifest: `android-app/verification/artifact-manifest.json` (ignored build evidence). Latest real browser screenshots: `/private/tmp/lastcore-ui-simple-1440.png`, `lastcore-ui-simple-768.png`, `lastcore-ui-simple-390.png`, `lastcore-ui-simple-local.png`, `lastcore-ui-simple-website.png`. These are browser renders, not Android device screenshots.

No production deployment, merge, publishing, release signing, Windows/Linux hardware execution or Android physical-device execution occurred. The existing stable release, frozen bundles and historic evidence remain independently identified. Local test/preview services were stopped after QA.
