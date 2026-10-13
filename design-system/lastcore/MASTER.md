# LastCore shared UI system

## Product and surface roles
LastCore connects multiple edge-device types. Android and macOS have current implementation paths; Windows/Linux source launchers are separate from verified binary distribution. Other edge adapters remain in development. Interfaces must distinguish tiny layer execution, single-device SmolLM2 jobs, and historical replay.

The existing product website (`demo/`) uses a warm paper canvas. Operational web/desktop panels (`controller-server/app/ui/`) and Android use deep green ink surfaces. Both share sage accents, the LastCore three-column mark, restrained typography, generous spacing, and explicit status language. Illustrations are explanatory art; never label them device screenshots or telemetry.

## Shared tokens
| Role | Marketing | Control surfaces / Android |
| --- | --- | --- |
| Canvas | #F6F7F2 | #101719 |
| Surface | #FFFFFF | #172024 |
| Main text | #1D2E27 | #F2F6ED |
| Supporting text | #53644F | #AAB9BF |
| Accent | #D7ECB4 | #D7ECB4 |
| On accent | #1D2E27 | #1D2E27 |
| Input border | #67746C | #687D86 |
| Focus | #5A7846 | #D7ECB4 |
| Destructive text | #A53128 | #FFB4AB |

Native system sans typography, monospace only for metadata. No font CDN. Body controls 14–16sp/px; headings 20–28sp (native), larger responsive marketing headings. Spacing 8/12/16/20/24/32dp or CSS px. Controls >=48dp/px, visible text labels, native button semantics, visible focus and pressed feedback. Card radius 18–22; input/button radius 10. Do not animate layout bounds. Respect reduced motion.

## State and data rules
- Before connection: use an em dash, never plausible invented metrics.
- Read-only console: explicit manual refresh, last fetch time in `Asia/Shanghai`, stale warning after failure; keep API credentials only in JS memory and clear the field after submit. Disconnect clears data and memory. No local/session storage or telemetry.
- Desktop: report process state only. A running subprocess does not prove registration, heartbeats, model readiness or cloud connectivity. The user explicitly starts/stops a node using a pre-existing private env file; credentials never enter the browser.
- Android: one shared executor, explicit import/register/start, visible in-progress and stopping feedback; leaving the Activity requests cancellation. Never claim a stop until the existing executor reports completion. Keep registration out of saved state/autofill.
- Marketing replay: preserve original evidence/source bindings and non-live labeling. Other edge adapters remain developmental.

## Reuse decisions
Installed `nextlevelbuilder/ui-ux-pro-max-skill` at fixed commit `7246805992a42b7bba37d4cac83b01416f0c6c44` under `.agents/skills/ui-ux-pro-max/` (MIT). Its monitoring-dashboard guidance supports status hierarchy and semantic tokens. The first broad query suggested an FAQ landing pattern; discarded it as unsuitable. The narrower monitoring result suggested glassmorphism; retain opaque surfaces for readability and continuity with the existing website. This file records the reviewed decisions rather than unreviewed search output.

Checked Material Components Android (maintenance mode) and pywebview (BSD-3-Clause). Migrating native Java to Compose or adding a native webview runtime would add unnecessary dependencies. Reuse Android's native Views/RippleDrawable, FastAPI/Starlette StaticFiles and Python's loopback HTTP server instead. No third-party UI runtime installed; existing Lucide assets in the product demo keep their existing ISC provenance.

## Verification
Run Controller console/auth regressions and desktop loopback security tests. Exercise the browser at 390/768/1440px, keyboard navigation, real API connection, error/disconnect states and empty data. Exercise a real local demo-node process for launch/stop; label demo mode as simulation. Compile native Android resources and Java with the pinned API35/JDK17 toolchain; full APK/lint/device verification must be reported separately. Physical Android verification remains deferred.

Source work is isolated on `codex/unified-ui` at `/private/tmp/lastcore-unified-ui`; it is not a deployed update. The project skill is included in that branch. The shared main checkout was restored after verifying a binary diff backup plus SHA256 matches for all 94 migrated files; unrelated files were left intact.

## Concise technology workflow
Use neutral charcoal control surfaces, sage only for the active primary action, 14px/dp card corners, and a compact workspace title. Do not put a marketing hero, orbit art or repeated bilingual metadata above operational controls. Preserve the website's established light composition.

Buttons use short verb labels and reveal only actions available in the current state. Web: connect -> compact connected status -> refresh/disconnect, with field/credentials cleared on disconnect/auth failure. Desktop: start -> stop -> stopping confirmation -> start; unknown outcomes permit a status retry only. Android: shared model preparation -> local/cloud mode -> one primary operation -> result and stop. First enrollment is expandable. Never gate cloud recovery on model presence. Field errors appear next to the relevant input; busy states disable competing actions; waiting-to-stop must remain distinct from stopped.
