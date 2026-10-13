# LastCore product demo

A Chinese static product showcase with original SVG/CSS device illustrations and an interactive **public historical-summary replay**. It never creates, claims, cancels or runs a model job. The current Controller deployment remains source `85ddad9`; this separate presentation branch does not redeploy the Controller. Current core links pin `09a02ef`, which includes repaired Mac Worker `30b9efc`; the Mac download points to that fixed GitHub source archive, while the immutable Alpha Android/download attachments retain their original build identity.

Run locally without dependencies:

```sh
python3 -m http.server 8766 --bind 127.0.0.1 --directory demo
```

Open `http://127.0.0.1:8766/`. Create a portable single HTML with `python3 demo/export-standalone.py`; open `demo/offline.html` directly. All page typography, device diagrams, icons, CSS, replay logic and historical-summary data are local. Download/source/API links and the optional public-health check need a network; failure never claims that the service is offline. The health button performs only an unauthenticated GET to `https://8.134.161.123/healthz`, checks JSON `status=ok`, and labels the current check in `Asia/Shanghai`. No automatic health probe or persistence is used.

Host the directory under `/demo/` using the existing HTTPS static-server route. The `lastcore-demo-kit.zip` download is a byte-for-byte server mirror of the single unified source/showcase ZIP produced by the submission owner, not a second independently packaged kit. That ZIP includes all project source, `demo/offline.html`, public evidence, five original browser PNGs and their capture metadata under root `screenshots/`, `demo/DEMO-SCRIPT-3MIN.txt`, the root README, `AI_USAGE.md`, `ai-logs/`, and archive manifests/checksums. The canonical archive and server mirror must have the same SHA256. APK binaries, model/cache files, private configuration and credentials are excluded. The existing `v0.1.0-alpha.1` attachments remain immutable.

## Evidence and boundaries

`evidence.json` preserves the public acceptance-summary source and URLs. `evidence.js` adapts only that published summary for rendering. Generation displays `Paris` as a summary, not a verbatim response. The displayed time is the public record's **verification time**, not a task creation time. Replay steps are explanatory, not a frame-by-frame execution log or real latency. Mac cloud generation/timeout/cancellation proof comes from Controller source `21c5d4b`; host JVM/JNI proof uses Android source `1ffc0bd` and Controller `b0b494b`. Source relationships and present deployment are separately recorded in the public release manifest. The additional public Mac recovery evidence comes from `submission/evidence/mac-worker-30b9efc-local.json` and its independent review at core09: 23 regressions, three actual local Controller synthetic recovery fixtures and one real local GGUF normal task. It is shown separately from the old cloud proof, with no timing/performance marketing. No private database query, private prompt export or new inference is involved.

Android host JNI execution is not Android device execution. Physical Android inference, framework/Keystore, lifecycle and device TLS remain deferred. The installable APK has a development signature; unsigned release cannot be installed directly. Mac whole-model execution is a Python CLI; the frozen legacy/tiny bundle has a separate scope and is not a pretrained model desktop app. Device art is explicitly a diagram.

## Design and reuse

Semantic HTML, native CSS Grid/Flexbox/SVG and bounded native JavaScript keep this page buildless and portable. Pico CSS was checked but its [official maintenance notice](https://picocss.com/docs/maintenance) says it is archived. A broad CSS/component framework would add unused behavior and override the bespoke device composition; no framework is installed. Sixteen small SVG icons reuse the active [Lucide](https://github.com/lucide-icons/lucide) release `1.55.0`, commit `70562c1ee1c4fdcf736fe97bc893fb8511927934`, under ISC. Full license and exact source/hashes are in `licenses/`. Original LastCore page code inherits the repository MIT license. No external fonts, CDN assets, analytics, image-generation claim or application credentials are included.

The page supports keyboard tab selection, explicit play/pause/reset, reduced-motion step-by-step playback, visibility pause, mobile layouts and print output. Browser QA is independent from syntax checks and must examine desktop/mobile screenshots plus real link/interaction behavior. Illustrations must never be presented as actual Android screenshots.
