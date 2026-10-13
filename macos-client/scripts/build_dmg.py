"""Build a source-bound, ad-hoc signed arm64 DMG on macOS; no notarization claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.1.1"


def command(*args, **kwargs):
    return subprocess.check_output(list(args), cwd=ROOT, **kwargs).decode().strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if command("uname", "-m") != "arm64":
        raise SystemExit("This release artifact is arm64 only; build on an arm64 Mac")
    if command("git", "status", "--porcelain", "--untracked-files=all", "--", "macos-client", "LICENSE"):
        raise SystemExit("Commit the client and license before preparing a release artifact")
    commit = command("git", "rev-parse", "HEAD")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    image = output / f"LastCoreClient-{VERSION}-arm64-{commit[:8]}.dmg"
    receipt = image.with_suffix(".json")
    checksum = image.with_suffix(".dmg.sha256")
    if any(p.exists() for p in (image, receipt, checksum)):
        raise SystemExit("Refusing to overwrite an existing release artifact")
    with tempfile.TemporaryDirectory(prefix="lastcore-client-dmg-") as folder:
        work = Path(folder)
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(work / "clang-cache"),
                   SWIFTPM_MODULECACHE_OVERRIDE=str(work / "module-cache"))
        subprocess.run(["swift", "build", "--package-path", str(ROOT / "macos-client"),
                        "--configuration", "release", "--scratch-path", str(work / "build"),
                        "--cache-path", str(work / "cache"), "--config-path", str(work / "config"),
                        "--security-path", str(work / "security"), "--disable-sandbox"], env=env, check=True)
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
        sources = {str(path.relative_to(ROOT)): digest(path)
                   for path in sorted((ROOT / "macos-client").rglob("*"))
                   if path.is_file() and ".build" not in path.parts}
        provenance = dict(source_commit=commit, version=VERSION, architecture="arm64",
                          imported_client_author_commit="9fef3e632b23a018f61e5fe2c94c610df7d87959",
                          source_files=sources, signing="ad-hoc", notarized=False,
                          swift_version=command("swift", "--version"))
        (resources / "source-binding.json").write_text(json.dumps(provenance, indent=2) + "\n")
        shutil.copy2(ROOT / "LICENSE", resources / "LICENSE.txt")
        os.symlink("/Applications", stage / "Applications")
        (stage / "安装说明.txt").write_text(
            "LastCoreClient 0.1.1 · Apple Silicon · macOS 13+\n"
            "将 LastCoreClient.app 拖入 Applications，退出旧软件后打开新版。\n"
            "本版本显示云端确认的实际线性计算结果；已有节点凭据仍由 macOS 钥匙串管理。\n"
            "这是 ad-hoc 签名开发版，尚未通过 Apple Developer ID 签名或公证。\n"
            "若 macOS 要求信任或钥匙串授权，请由用户本人核对来源并操作。\n")
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
        binary_hash = digest(executable)
        subprocess.run(["hdiutil", "create", "-srcfolder", str(stage), "-volname", "LastCoreClient 0.1.1",
                        "-format", "UDZO", str(image)], check=True)
        subprocess.run(["hdiutil", "verify", str(image)], check=True)
        provenance.update(dmg_sha256=digest(image), dmg_bytes=image.stat().st_size,
                          executable_sha256=binary_hash)
        receipt.write_text(json.dumps(provenance, indent=2) + "\n")
        checksum.write_text(f"{provenance['dmg_sha256']}  {image.name}\n")
        print(json.dumps({"image": str(image), "receipt": str(receipt),
                          "sha256": provenance["dmg_sha256"], "source_commit": commit}, indent=2))


if __name__ == "__main__":
    main()
