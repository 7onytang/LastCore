"""Build a source-bound arm64 DMG; development by default, opt-in owner notarization."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
from uuid import UUID
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.1.1"


def command_bytes(*args, **kwargs):
    try:
        return subprocess.check_output(list(args), cwd=ROOT, stderr=kwargs.pop("stderr", subprocess.PIPE), **kwargs)
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError(f"{args[0]} failed; captured tool output is withheld") from None


def command(*args, **kwargs):
    return command_bytes(*args, **kwargs).decode().strip()


def export_snapshot(commit, destination):
    # Read tree/blob objects directly: no index, worktree, filters or export attributes.
    listing = command_bytes("git", "ls-tree", "-r", "-z", commit, "--", "macos-client", "LICENSE")
    hashes, blobs = {}, {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        header, name_bytes = entry.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        name = name_bytes.decode()
        path = PurePosixPath(name)
        if (kind != "blob" or mode not in ("100644", "100755")
                or path.is_absolute() or ".." in path.parts
                or not (name == "LICENSE" or name.startswith("macos-client/"))):
            raise RuntimeError("Source snapshot requires regular tracked client/license files")
        data = command_bytes("git", "cat-file", "blob", oid)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o555 if mode == "100755" else 0o444)
        hashes[name] = hashlib.sha256(data).hexdigest()
        blobs[name] = oid
    if "LICENSE" not in hashes or "macos-client/Package.swift" not in hashes:
        raise RuntimeError("Fixed source snapshot lacks the client package or license")
    return hashes, blobs


def run_command(args, **kwargs):
    try:
        return subprocess.run(args, cwd=kwargs.pop("cwd", ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError(f"{args[0]} failed; captured tool output is withheld") from None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def production_options(parser, args):
    values = (args.signing_identity, args.team_id, args.notary_profile, args.certificate_sha256)
    if not args.production:
        if any(values):
            parser.error("Signing options require explicit --production")
        return
    if not all(values):
        parser.error("--production requires --signing-identity, --team-id, --notary-profile and --certificate-sha256")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.certificate_sha256):
        parser.error("--certificate-sha256 must be the expected public leaf certificate DER SHA256")
    if not re.fullmatch(r"[A-Z0-9]{10}", args.team_id):
        parser.error("--team-id must be the ten-character Apple Team ID")
    if (not args.signing_identity.startswith("Developer ID Application: ")
            or not args.signing_identity.endswith(f"({args.team_id})")):
        parser.error("Use the full Developer ID Application identity matching --team-id")
    if not args.notary_profile.strip() or args.notary_profile.startswith("-"):
        parser.error("--notary-profile must name an existing owner-configured profile")


def verify_developer_id(path, args, *, runtime=False):
    requirement = (f'anchor apple generic and certificate leaf[subject.OU] = "{args.team_id}" '
                   'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    run_command(["codesign", "--verify", "--strict", "-R", requirement, str(path)])
    with tempfile.TemporaryDirectory(prefix="lastcore-public-certificate-") as folder:
        prefix = Path(folder) / "certificate"
        run_command(["codesign", "--display", "--extract-certificates", str(prefix), str(path)])
        leaf = Path(str(prefix) + "0")
        if not leaf.is_file() or digest(leaf) != args.certificate_sha256.lower():
            raise RuntimeError("Actual public signing certificate SHA256 differs from owner expectation")
    detail = command("codesign", "--display", "--verbose=4", str(path), stderr=subprocess.STDOUT)
    lines = detail.splitlines()
    if (f"Authority={args.signing_identity}" not in lines
            or f"TeamIdentifier={args.team_id}" not in lines
            or not any(line.startswith("Timestamp=") and line.partition("=")[2].strip() not in ("", "none") for line in lines)
            or (runtime and not any("flags=" in line and "runtime" in line for line in lines))):
        raise RuntimeError("Actual signature does not match Developer ID, team, timestamp or hardened runtime")


def notarize_image(image, args):
    raw = command("xcrun", "notarytool", "submit", str(image),
                  "--keychain-profile", args.notary_profile, "--wait", "--output-format", "json")
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise RuntimeError("Apple notarization returned an unreadable response; output withheld") from None
    if not isinstance(result, dict) or result.get("status") != "Accepted":
        raise RuntimeError("Apple notarization was not Accepted; no production artifact published")
    try:
        submission_id = str(UUID(result["id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise RuntimeError("Apple notarization did not return a valid submission ID") from None
    run_command(["xcrun", "stapler", "staple", str(image)])
    run_command(["xcrun", "stapler", "validate", str(image)])
    verify_developer_id(image, args)
    run_command(["hdiutil", "verify", str(image)])
    return submission_id


def publish_files(files):
    # Exclusive creation preserves existing artifacts even during a publication race.
    created = []
    try:
        for source, destination in files:
            with destination.open("xb") as target:
                created.append(destination)
                with source.open("rb") as original:
                    shutil.copyfileobj(original, target)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--production", action="store_true", help="Owner-only Developer ID signing and Apple notarization")
    parser.add_argument("--signing-identity", help="Full existing Developer ID Application identity")
    parser.add_argument("--team-id", help="Expected Apple Team ID; checked against actual signatures")
    parser.add_argument("--certificate-sha256", help="Expected public leaf signing certificate DER SHA256")
    parser.add_argument("--notary-profile", help="Existing notarytool Keychain profile name; no credentials accepted")
    args = parser.parse_args()
    production_options(parser, args)
    if command("uname", "-m") != "arm64":
        raise SystemExit("This release artifact is arm64 only; build on an arm64 Mac")
    if command("git", "status", "--porcelain", "--untracked-files=all", "--", "macos-client", "LICENSE"):
        raise SystemExit("Commit the client and license before preparing a release artifact")
    commit = command("git", "rev-parse", "HEAD")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    channel = "-production" if args.production else ""
    image = output / f"LastCoreClient-{VERSION}-arm64-{commit[:8]}{channel}.dmg"
    receipt = image.with_suffix(".json")
    checksum = image.with_suffix(".dmg.sha256")
    if any(p.exists() for p in (image, receipt, checksum)):
        raise SystemExit("Refusing to overwrite an existing release artifact")
    with tempfile.TemporaryDirectory(prefix="lastcore-client-dmg-") as folder:
        work = Path(folder)
        candidate = work / image.name
        snapshot = work / "source"
        sources, blobs = export_snapshot(commit, snapshot)
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(work / "clang-cache"),
                   SWIFTPM_MODULECACHE_OVERRIDE=str(work / "module-cache"))
        run_command(["swift", "build", "--package-path", str(snapshot / "macos-client"),
                        "--configuration", "release", "--scratch-path", str(work / "build"),
                        "--cache-path", str(work / "cache"), "--config-path", str(work / "config"),
                        "--security-path", str(work / "security"), "--disable-sandbox"], env=env, cwd=snapshot)
        stage = work / "stage"
        app = stage / "LastCoreClient.app"
        executable = app / "Contents/MacOS/LastCoreClient"
        resources = app / "Contents/Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir()
        shutil.copy2(work / "build/arm64-apple-macosx/release/LastCoreClient", executable)
        info = dict(CFBundleExecutable="LastCoreClient", CFBundleIdentifier="ai.lastcore.client",
                    CFBundleName="LastCoreClient", CFBundleDisplayName="LastCoreClient",
                    CFBundlePackageType="APPL", CFBundleShortVersionString=VERSION,
                    CFBundleVersion="1", LSMinimumSystemVersion="13.0", NSHighResolutionCapable=True)
        with (app / "Contents/Info.plist").open("wb") as file:
            plistlib.dump(info, file)
        provenance = dict(source_commit=commit, version=VERSION, architecture="arm64",
                          imported_client_author_commit="9fef3e632b23a018f61e5fe2c94c610df7d87959",
                          source_files=sources, source_git_blobs=blobs, signing="developer-id-requested" if args.production else "ad-hoc", notarized=False,
                          swift_version=command("swift", "--version"))
        # Embedded binding predates signing: never claims a completed notarization.
        (resources / "source-binding.json").write_text(json.dumps(provenance, indent=2) + "\n")
        shutil.copy2(snapshot / "LICENSE", resources / "LICENSE.txt")
        os.symlink("/Applications", stage / "Applications")
        (stage / "安装说明.txt").write_text(
            "LastCoreClient 0.1.1 · Apple Silicon · macOS 13+\n"
            "将 LastCoreClient.app 拖入 Applications，退出旧软件后打开新版。\n"
            "本版本显示云端确认的实际线性计算结果；已有节点凭据仍由 macOS 钥匙串管理。\n" +
            ("生产包仅在 Developer ID 验签、公证与 DMG ticket 验证通过后发布；核对旁附 JSON。\n"
             if args.production else "这是 ad-hoc 签名开发版，尚未通过 Apple Developer ID 签名或公证。\n") +
            "若 macOS 要求信任或钥匙串授权，请由用户本人核对来源并操作。\n")
        if args.production:
            run_command(["codesign", "--force", "--sign", args.signing_identity,
                            "--options", "runtime", "--timestamp", str(app)])
            verify_developer_id(app, args, runtime=True)
        else:
            run_command(["codesign", "--force", "--sign", "-", str(app)])
            run_command(["codesign", "--verify", "--strict", str(app)])
        binary_hash = digest(executable)
        run_command(["hdiutil", "create", "-srcfolder", str(stage), "-volname", "LastCoreClient 0.1.1",
                        "-format", "UDZO", str(candidate)])
        run_command(["hdiutil", "verify", str(candidate)])
        if args.production:
            run_command(["codesign", "--force", "--sign", args.signing_identity,
                            "--timestamp", str(candidate)])
            verify_developer_id(candidate, args)
            submission_id = notarize_image(candidate, args)
            provenance.update(release_channel="production", signing="developer-id",
                              notarized=True, notarization_id=submission_id,
                              team_id=args.team_id, certificate_sha256=args.certificate_sha256.lower(),
                              app_signature_verified=True,
                              hardened_runtime_verified=True, dmg_signature_verified=True,
                              staple_validated=True)
        else:
            provenance.update(release_channel="development", app_signature_verified=True,
                              dmg_signature_verified=False, staple_validated=False)
        provenance.update(dmg_sha256=digest(candidate), dmg_bytes=candidate.stat().st_size,
                          executable_sha256=binary_hash)
        staged_receipt = work / receipt.name
        staged_checksum = work / checksum.name
        staged_receipt.write_text(json.dumps(provenance, indent=2) + "\n")
        staged_checksum.write_text(f"{provenance['dmg_sha256']}  {image.name}\n")
        publish_files([(candidate, image), (staged_receipt, receipt), (staged_checksum, checksum)])
        print(json.dumps({"image": str(image), "receipt": str(receipt),
                          "sha256": provenance["dmg_sha256"], "source_commit": commit}, indent=2))


if __name__ == "__main__":
    main()
