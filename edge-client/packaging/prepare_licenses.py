"""Build-time license collection only; never imports or launches edge client code."""
import hashlib
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import shutil
import ssl
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def main():
    out = Path(sys.argv[1]);out.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("existing_notices", ROOT / "node-agent/desktop/collect_licenses.py")
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    closure = module.runtime_closure(["httpx"])
    entries = []
    for name, distribution in sorted(closure.items()):
        files = module.license_files(distribution)
        if not files:raise RuntimeError("Missing complete dependency license: " + name)
        for relative in files:
            source = Path(distribution.locate_file(relative))
            target = out / "dependencies" / name / Path(relative).name
            target.parent.mkdir(parents=True, exist_ok=True);shutil.copyfile(source, target)
        entries.append({"name":name,"version":distribution.version,
                        "license":module.license_label(distribution.metadata),
                        "project_url":module.project_url(distribution.metadata)})
    python_license = module.locate_cpython_license()
    shutil.copyfile(python_license, out / "CPython-LICENSE.txt")
    if module.PSF_LICENSE_HEADING not in (out / "CPython-LICENSE.txt").read_bytes():
        raise RuntimeError("Incomplete CPython license")
    for name in ("pyinstaller", "pyinstaller-hooks-contrib"):
        distribution=metadata.distribution(name)
        files=module.license_files(distribution)
        if not files:raise RuntimeError("Missing build/runtime-hook terms: " + name)
        for relative in files:
            target=out / "build-tools" / name / Path(relative).name
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(distribution.locate_file(relative),target)
    for component in ("tcl", "tk"):
        candidates = list((Path(sys.base_prefix) / "tcl").glob(component+"*/license.terms"))
        candidates += [Path("/usr/share/doc") / (component+"8.6") / "copyright"]
        source = next((p for p in candidates if p.is_file()), None)
        if source is None and sys.platform=='win32' and sys.version_info[:3]==(3,13,16):
            source=ROOT / f"edge-client/packaging/vendor-licenses/{component}-8.6.15-license.terms"
        if source is None or not source.is_file():raise RuntimeError("Missing full Tcl/Tk notice: " + component)
        shutil.copyfile(source, out / (component.upper()+"-LICENSE.txt"))
    # TLS backend notice tracks the actual build interpreter's OpenSSL version.
    openssl_version=ssl.OPENSSL_VERSION.split()[1]
    license_url=f"https://raw.githubusercontent.com/openssl/openssl/openssl-{openssl_version}/LICENSE.txt"
    data=urllib.request.urlopen(license_url,timeout=30).read()
    if len(data)<500:raise RuntimeError("Incomplete OpenSSL terms")
    (out / "OpenSSL-LICENSE.txt").write_bytes(data)
    for notice in ('NOTICE','README.md'):
        url=f"https://raw.githubusercontent.com/openssl/openssl/openssl-{openssl_version}/{notice}"
        try:
            (out / ('OpenSSL-'+notice.replace('.','-')+'.txt')).write_bytes(urllib.request.urlopen(url,timeout=30).read())
        except urllib.error.HTTPError as exc:
            if exc.code!=404:raise
    shutil.copyfile(ROOT / "LICENSE", out / "LastCore-LICENSE.txt")
    notices=['Third-party source availability and license notices',
             'Full license files are preserved beside this notice.']
    for entry in entries:
        notices.append(f"{entry['name']} {entry['version']}: {entry['license']}; upstream {entry['project_url']}")
        if 'MPL' in entry['license'].upper():
            notices.append('Source Code Form for this exact version is available from the upstream project/tag above; no MPL-covered files are modified.')
    (out/'THIRD-PARTY-NOTICES.txt').write_text('\n'.join(notices)+'\n')
    manifest={"python_version":sys.version.split()[0],"runtime_distributions":entries,
              "openssl_version":openssl_version,"openssl_license_source":license_url,
              "source_paths_excluded":True,
              "files":{p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(out.rglob('*')) if p.is_file()}}
    (out / "manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")


if __name__=="__main__":main()
