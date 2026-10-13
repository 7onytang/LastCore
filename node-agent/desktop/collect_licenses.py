#!/usr/bin/env python3
"""Third-party license material collector for the node-agent desktop bundle.

Two modes:

- ``collect`` (run by build_bundle.sh BEFORE PyInstaller): derives the frozen
  runtime third-party dependency closure of ``node-agent/requirements.txt``
  from the BUILD venv's installed distribution metadata (importlib.metadata +
  packaging markers), verifies every distribution ships license files inside
  its ``.dist-info``, and writes ``build/licenses/manifest.json`` plus
  ``build/licenses/third-party-notices.md``. The spec then transports each
  distribution's dist-info (METADATA + license texts) into the bundle via
  PyInstaller's ``copy_metadata()`` hook utility and adds the notices
  document under ``_internal/licenses/``. It also copies the build
  interpreter's own full CPython license text (PSF License Version 2 plus
  the BeOpen/CNRI/CWI predecessor terms and the 0-Clause BSD documentation
  license) verbatim into the same directory, so the embedded libpython
  travels with its complete license text, not just a URL.

- ``verify`` (run by build_bundle.sh AFTER PyInstaller): reads the PyInstaller
  Analysis TOC files to enumerate the distributions actually frozen into the
  bundle, cross-checks them against the manifest (any missing or extra frozen
  third-party distribution fails the build), checks that every license
  file recorded in the manifest plus the generated notices document really
  exist inside the bundle directory, and checks the bundled CPython license
  text exists, is non-empty, carries the PSF-2.0 heading, hashes to the
  value recorded at collect time, and is named for the embedded Python
  version; the notices document must point at that file.

certifi is MPL-2.0; the generated notices include the MPL-2.0 section 3.2
source-availability statement pointing at the exact upstream source project.

Exit codes: 0 ok; 1 license material missing; 2 frozen-content mismatch;
3 bad usage/environment (missing manifest, unparseable TOC, ...).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata as importlib_metadata
import json
import platform
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

LICENSE_STEMS = {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
PSF_LICENSE_HEADING = b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2"


def locate_cpython_license() -> Path:
    """Full CPython license text shipped inside the build interpreter's install.

    The build interpreter itself is the authoritative source: its install tree
    carries the complete composite CPython license that covers the libpython
    PyInstaller embeds into the bundle (PSF License Version 2 plus the
    BeOpen/CNRI/CWI predecessor terms and the 0-Clause BSD documentation
    license). A URL is NOT a substitute - the caller fails the build if this
    file cannot be found.
    """
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        Path(sys.base_prefix) / "lib" / f"python{minor}" / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.prefix) / "lib" / f"python{minor}" / "LICENSE.txt",
        Path(sys.prefix) / "LICENSE.txt",
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    raise FileNotFoundError(
        "no CPython license text found in the build interpreter install tree "
        f"(looked at: {', '.join(str(p) for p in candidates)}); refusing to "
        "publish a bundle whose embedded libpython ships only a license URL")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def find_dist(name: str):
    try:
        return importlib_metadata.distribution(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def license_label(meta) -> str:
    expr = meta.get("License-Expression") or meta.get("License") or ""
    if expr.strip():
        return expr.strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in (meta.get_all("Classifier") or [])
        if c.startswith("License ::")
    ]
    return " / ".join(classifiers) if classifiers else "UNKNOWN"


def project_url(meta) -> str:
    entries = []
    for raw in meta.get_all("Project-URL") or []:
        label, _, url = str(raw).partition(",")
        entries.append((label.strip().lower(), url.strip()))
    for label, url in entries:
        if label in {"source", "repository", "homepage", "home"}:
            return url
    for _label, url in entries:
        return url
    return (meta.get("Home-page") or "").strip()


def license_files(dist) -> list[str]:
    """Relative paths of license files inside the distribution's dist-info."""
    found = []
    for entry in dist.files or []:
        parts = PurePosixPath(str(entry)).parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        stem = parts[-1].split(".")[0].upper()
        if stem in LICENSE_STEMS or "licenses" in parts[1:-1]:
            found.append(str(entry))
    return sorted(found)


def runtime_closure(roots: list[str]) -> dict:
    """{canonical_name: dist} for the runtime closure of requirements.txt."""
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    env = default_environment()
    env["extra"] = ""
    closure: dict = {}
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        dist = find_dist(name)
        if dist is None:
            print(f"collect_licenses: distribution {name!r} not installed in build venv",
                  file=sys.stderr)
            raise SystemExit(1)
        closure[name] = dist
        for req_string in dist.requires or []:
            req = Requirement(req_string)
            if req.marker and not req.marker.evaluate(env):
                continue
            if req.extras:
                continue
            stack.append(canonical(req.name))
    return closure


def root_requirements(requirements_path: Path) -> list[str]:
    roots = []
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = line.split(";")[0].strip()
        name = re.split(r"[=<>!~\[ ]", req, maxsplit=1)[0]
        if name:
            roots.append(canonical(name))
    return roots


def lock_names(lock_path: Path) -> set[str]:
    names = set()
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(canonical(line.split("==")[0]))
    return names


def collect_distribution(dist, source_note: str | None) -> dict:
    files = license_files(dist)
    entry = {
        "name": canonical(dist.metadata["Name"]),
        "display_name": dist.metadata["Name"],
        "version": dist.version,
        "license": license_label(dist.metadata),
        "license_files": files,
        "license_sha256": {rel: hashlib.sha256(dist.locate_file(rel).read_bytes()).hexdigest()
                           for rel in files},
        "native_binaries": {str(rel): hashlib.sha256(dist.locate_file(rel).read_bytes()).hexdigest()
                            for rel in dist.files or []
                            if str(rel).endswith((".so", ".dylib"))},
        "project_url": project_url(dist.metadata),
        "dist_info": f"{dist.metadata['Name'].replace('-', '_')}-{dist.version}.dist-info",
        "source_availability": source_note,
    }
    return entry


def cmd_collect(args) -> int:
    requirements_path = Path(args.requirements).resolve()
    lock_path = Path(args.lock).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    roots = root_requirements(requirements_path)
    if not roots:
        print("collect_licenses: no root requirements found", file=sys.stderr)
        return 3
    closure = runtime_closure(roots)

    locked = lock_names(lock_path)
    not_locked = sorted(set(closure) - locked)
    if not_locked:
        print("collect_licenses: runtime distributions missing from build lock: "
              + ", ".join(not_locked), file=sys.stderr)
        return 2

    runtime_entries = []
    for name in sorted(closure):
        dist = closure[name]
        files = license_files(dist)
        if not files:
            print(f"collect_licenses: {name} {dist.version} ships no license file "
                  "in its dist-info; refusing to bundle without license material",
                  file=sys.stderr)
            return 1
        note = None
        if "MPL" in license_label(dist.metadata).upper():
            url = project_url(dist.metadata) or "https://pypi.org/project/" + name
            note = (
                f"MPL-2.0 section 3.2 source availability: the Source Code Form of "
                f"{dist.metadata['Name']} {dist.version} is available upstream at "
                f"{url} (source repository; the tagged release corresponding to this "
                f"version provides the exact source). The full MPL-2.0 license text "
                f"ships inside this bundle at _internal/{files[0]}."
            )
        runtime_entries.append(collect_distribution(dist, note))

    build_tool = find_dist("pyinstaller")
    if build_tool is None:
        print("collect_licenses: pyinstaller not installed in build venv", file=sys.stderr)
        return 3
    build_entry = collect_distribution(build_tool,
                                       "The PyInstaller license is GPL-2.0-or-later "
                                       "with a special exception that allows using "
                                       "PyInstaller to build and distribute non-free "
                                       "programs; its bootloader is embedded in both "
                                       "bundled executables.")

    try:
        cpython_license_src = locate_cpython_license()
    except FileNotFoundError as exc:
        print(f"collect_licenses: {exc}", file=sys.stderr)
        return 3
    cpython_version = platform.python_version()
    cpython_rel = f"licenses/CPython-{cpython_version}-LICENSE.txt"
    cpython_dest = output_dir / f"CPython-{cpython_version}-LICENSE.txt"
    shutil.copyfile(cpython_license_src, cpython_dest)
    cpython_bytes = cpython_dest.read_bytes()
    cpython_sha256 = hashlib.sha256(cpython_bytes).hexdigest()
    if PSF_LICENSE_HEADING not in cpython_bytes:
        print("collect_licenses: interpreter license text does not contain the "
              "PSF License Version 2 heading; refusing to ship it as the "
              "embedded CPython's license material", file=sys.stderr)
        return 1

    manifest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.mac_ver()[0]} {platform.machine()}",
        "requirements_root": str(requirements_path),
        "lock_file": str(lock_path),
        "runtime_distributions": runtime_entries,
        "build_tool": build_entry,
        "python_runtime": {
            "version": cpython_version,
            "license": "PSF-2.0 (composite CPython license: PSF-2.0 plus the "
                       "BeOpen, CNRI and CWI predecessor terms and the "
                       "0-Clause BSD license for documentation code)",
            "url": "https://www.python.org/psf/license/",
            "license_file": cpython_rel,
            "source_path": str(cpython_license_src),
            "source_sha256": cpython_sha256,
            "size_bytes": len(cpython_bytes),
            "interpreter_prefix": sys.base_prefix,
            "note": "libpython is embedded in the bundle by PyInstaller; the "
                    "full CPython license text is copied verbatim from this "
                    "build interpreter's install tree into _internal/.",
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Third-party notices for the LastCore node-agent desktop bundle",
        "",
        f"Generated {manifest['generated_at']} by node-agent/desktop/collect_licenses.py",
        f"in the build venv (Python {manifest['python_version']}, {manifest['platform']}).",
        "Distribution versions match node-agent/desktop/requirements-build-lock.txt;",
        "each distribution's full dist-info (METADATA + license files) is bundled under",
        "`_internal/` by the spec via PyInstaller's copy_metadata().",
        "",
        "## Runtime dependencies (frozen into the bundle)",
        "",
    ]
    for entry in runtime_entries:
        lines.append(f"### {entry['display_name']} {entry['version']} — {entry['license']}")
        lines.append("")
        if entry["project_url"]:
            lines.append(f"- Project / source: {entry['project_url']}")
        for rel in entry["license_files"]:
            lines.append(f"- License text in bundle: `_internal/{rel}`")
        if entry["source_availability"]:
            lines.append(f"- {entry['source_availability']}")
        lines.append("")

    lines += [
        "## Build tooling embedded in the executables",
        "",
        f"### PyInstaller {build_entry['version']} — {build_entry['license']}",
        "",
        "- The PyInstaller bootloader is embedded in both bundled executables; the",
        "  special exception in its license allows distributing non-free programs",
        "  built with PyInstaller.",
    ]
    for rel in build_entry["license_files"]:
        lines.append(f"- License text in bundle: `_internal/{rel}`")
    lines += [
        "",
        "## Python runtime",
        "",
        f"- CPython {manifest['python_runtime']['version']} (libpython embedded in "
        "both executables by PyInstaller).",
        f"- Full license text bundled at: `_internal/{cpython_rel}` "
        f"({len(cpython_bytes)} bytes, sha256 `{cpython_sha256}`), copied verbatim "
        f"from the build interpreter's install tree at {cpython_license_src} "
        f"(interpreter prefix {sys.base_prefix}).",
        f"- License: {manifest['python_runtime']['license']} "
        f"(reference: {manifest['python_runtime']['url']}).",
        "",
    ]
    notices_path = output_dir / "third-party-notices.md"
    notices_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"collect_licenses: runtime closure: "
          + ", ".join(f"{e['name']}=={e['version']}" for e in runtime_entries))
    print(f"collect_licenses: CPython {cpython_version} license text: "
          f"{cpython_rel} (sha256 {cpython_sha256})")
    print(f"collect_licenses: wrote {manifest_path}")
    print(f"collect_licenses: wrote {notices_path}")
    return 0


MODULE_KINDS = {"PYMODULE", "PYSOURCE", "EXTENSION"}


def _iter_module_names(obj):
    """Yield module names from (name, path, kind) entries nested in a TOC."""
    if (isinstance(obj, tuple) and len(obj) == 3
            and all(isinstance(x, str) for x in obj) and obj[2] in MODULE_KINDS):
        yield obj[0]
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_module_names(item)


def frozen_distributions(workpath: Path) -> set[str]:
    """Third-party distributions actually frozen, from the Analysis TOC files.

    PyInstaller stores Analysis-*.toc as Python literal text; the format's
    nesting has changed between versions, so walk it defensively instead of
    relying on a fixed layout.
    """
    toc_paths = sorted(workpath.glob("*/Analysis-*.toc"))
    if not toc_paths:
        raise FileNotFoundError(f"no Analysis-*.toc under {workpath}")
    import ast

    top_modules: set[str] = set()
    for toc in toc_paths:
        data = ast.literal_eval(toc.read_text(encoding="utf-8", errors="strict"))
        top_modules.update(_iter_module_names(data))
    mapping = importlib_metadata.packages_distributions()
    dists: set[str] = set()
    for top in top_modules:
        for dist_name in mapping.get(top.split(".")[0], []):
            dists.add(canonical(dist_name))
    return dists


def cmd_verify(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    bundle = Path(args.bundle_dir).resolve()
    internal = bundle / "_internal"
    problems: list[str] = []
    rc = 0

    try:
        frozen = frozen_distributions(Path(args.workpath).resolve())
    except FileNotFoundError as exc:
        print(f"collect_licenses verify: {exc}", file=sys.stderr)
        return 3

    expected = {e["name"] for e in manifest["runtime_distributions"]}
    extra = sorted(frozen - expected)
    missing = sorted(expected - frozen)
    if extra:
        problems.append(f"frozen but not in manifest: {', '.join(extra)}")
    if missing:
        problems.append(f"in manifest but not frozen: {', '.join(missing)}")

    checks = []
    for entry in manifest["runtime_distributions"]:
        checks.append((entry["name"], entry["license_files"], entry["license_sha256"]))
    checks.append(("pyinstaller", manifest["build_tool"]["license_files"],
                   manifest["build_tool"]["license_sha256"]))
    for name, rel_files, hashes in checks:
        if not rel_files:
            problems.append(f"{name}: manifest records no license files")
            continue
        for rel in rel_files:
            target = internal / rel
            if not target.is_file():
                problems.append(f"{name}: license file missing in bundle: {target}")
            elif hashlib.sha256(target.read_bytes()).hexdigest() != hashes[rel]:
                problems.append(f"{name}: license sha256 mismatch: {rel}")

    import ast
    native_checked = []
    def entries(obj):
        if isinstance(obj, tuple) and len(obj) == 3 and all(isinstance(x, str) for x in obj):
            yield obj
        elif isinstance(obj, (tuple, list)):
            for item in obj:
                yield from entries(item)
    # COLLECT TOC gives final destinations; Analysis gives module names instead.
    collect_tocs = list(Path(args.workpath).glob("*/COLLECT-*.toc"))
    if not collect_tocs:
        problems.append("no final COLLECT TOC found")
    for toc in collect_tocs:
        for dest, source, kind in entries(ast.literal_eval(toc.read_text())):
            if kind not in {"BINARY", "EXTENSION"}:
                continue
            for entry in manifest["runtime_distributions"]:
                dist = find_dist(entry["name"])
                for rel, source_hash in entry["native_binaries"].items():
                    if Path(source).resolve() == Path(dist.locate_file(rel)).resolve():
                        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != source_hash:
                            problems.append(f"native TOC source sha256 mismatch: {rel}")
                        target = internal / dest
                        if not target.is_file():
                            problems.append(f"native binary missing: {dest}")
                        else:
                            # PyInstaller may rewrite Mach-O load commands/signatures.
                            # Hash final binaries for evidence, validate original TOC provenance.
                            native_checked.append({"distribution": entry["name"], "file": dest,
                                                   "source_sha256": source_hash,
                                                   "bundle_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    if "numpy" in expected and not any(x["distribution"] == "numpy" for x in native_checked):
        problems.append("NumPy manifest exists but no NumPy native binary is frozen")

    py = manifest.get("python_runtime") or {}
    py_rel = py.get("license_file")
    py_info = None
    if not py_rel:
        problems.append("manifest python_runtime records no bundled CPython license file")
    else:
        target = internal / py_rel
        if not target.is_file() or target.stat().st_size == 0:
            problems.append(f"CPython license file missing or empty in bundle: {target}")
        else:
            data = target.read_bytes()
            if PSF_LICENSE_HEADING not in data:
                problems.append("bundled CPython license text lacks the "
                                "PSF License Version 2 heading")
            expected_sha = py.get("source_sha256")
            actual_sha = hashlib.sha256(data).hexdigest()
            if expected_sha and actual_sha != expected_sha:
                problems.append(f"bundled CPython license text sha256 mismatch: "
                                f"manifest {expected_sha} != bundle {actual_sha}")
            version = str(py.get("version") or "")
            if version and version not in Path(py_rel).name:
                problems.append(f"bundled CPython license file name does not record "
                                f"the embedded Python version {version}: {py_rel}")
            py_info = {"file": str(target), "sha256": actual_sha,
                       "size_bytes": len(data),
                       "version": version or py.get("version")}

    notices = internal / "licenses" / "third-party-notices.md"
    if not notices.is_file():
        problems.append(f"notices document missing in bundle: {notices}")
    elif py_rel and py_rel not in notices.read_text(encoding="utf-8"):
        problems.append("notices document does not point at the bundled CPython "
                        f"license file {py_rel}")

    result = {
        "bundle_dir": str(bundle),
        "frozen_third_party": sorted(frozen),
        "manifest_runtime": sorted(expected),
        "python_license": py_info,
        "native_binaries": native_checked,
        "license_checks_failed": problems,
        "passed": not problems,
    }
    print(json.dumps(result, indent=2))
    if problems:
        rc = 2 if (extra or missing) else 1
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_collect = sub.add_parser("collect", help="write manifest + notices (pre-build)")
    p_collect.add_argument("--requirements", required=True,
                           help="node-agent/requirements.txt")
    p_collect.add_argument("--lock", required=True,
                           help="desktop/requirements-build-lock.txt")
    p_collect.add_argument("--output-dir", required=True,
                           help="output directory (build/licenses)")
    p_collect.set_defaults(func=cmd_collect)

    p_verify = sub.add_parser("verify", help="verify frozen bundle (post-build)")
    p_verify.add_argument("--manifest", required=True)
    p_verify.add_argument("--bundle-dir", required=True)
    p_verify.add_argument("--workpath", required=True,
                          help="PyInstaller workpath (contains Analysis-*.toc)")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
