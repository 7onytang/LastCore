# Unified UI verification

Implementation baseline: `fd007a5c51d4e8b91a338c9f896006849591d8e9`, branch `codex/unified-ui`. Follow-up UI-only changes add the remote-HTTP credential guard and contain decorative tablet geometry; Android source is unchanged from the baseline.

- Controller console plus existing controller regressions: **19 passed**. Real temporary SQLite, public asset access, private API authentication, traversal rejection, and unchanged health behavior.
- Desktop security/configuration/node lifecycle regressions: **28 passed**. Loopback server rejects missing session, foreign Origin and foreign Host. No credentials in browser assets.
- Real local browser/HTTP loop: the desktop panel launched a configured **demo-mode** node; an isolated real Controller received its registration/heartbeat. The same UI stopped it with **exit code 0**. This is process/protocol proof, not model execution or production proof.
- Browser UI exercised the actual Controller APIs, empty task lists, registered-node data, clearing the input after connection, explicit disconnect/data clearing, invalid-credential feedback, website history-tab switching, and mobile 48px control targets. 1440/768/390px layouts checked. No fabricated metrics or persisted API token.
- Android full `build-apk.sh`: **BUILD SUCCESSFUL**, debug/unsigned-release/instrumentation APKs, `lintDebug`, artifact/source/license binding, 7 admission checks and 9 pure Java HTTPS checks passed. APK sources bind exactly to `fd007a5...`. Instrumentation was compiled, **not executed**. Physical Android checks remain deferred.
- Debug APK SHA256: `51a82a28296fcf2ffb6176293d245926da23533bb934fd387511bd7ccd3d422c` (development signature).
- Unsigned release SHA256: `2e26c7102d8d469523c055564aeaa551fb32017e078073742797a6d81389f978`.
- Instrumentation APK SHA256: `ca198d00232fc5e15bc41825c1c5b64e0cc49a0a07e8ea498932fc43bcab4a71`.

Local full build log: `/private/tmp/lastcore-unified-ui-android-build.log`; artifact details: `android-app/verification/artifact-manifest.json` (ignored build evidence). Browser PNGs: `/private/tmp/lastcore-ui-final-1440.png`, `lastcore-ui-final-768.png`, `lastcore-ui-final-390.png`, `lastcore-ui-local-panel.png`, `lastcore-ui-website-mobile.png`. Screenshots are actual browser renders, not Android device screenshots.

No production deployment, publishing, native signing, Windows/Linux hardware execution or physical Android execution occurred. Current stable website historical evidence and artifact identities remain separate. The startup/test servers are closed after QA.
