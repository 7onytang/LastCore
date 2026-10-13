"""Two native onedir entry points, using maintained official Tk/certifi hooks."""
import json
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root=Path(SPECPATH).resolve().parents[1]
licenses=root / "edge-client/build/licenses"
manifest=json.loads((licenses / "manifest.json").read_text())
datas=[(str(licenses),"licenses")]
for entry in manifest["runtime_distributions"]:
    datas+=copy_metadata(entry["name"])
analyses=[]
executables=[]
for name,entry,console in [("LastCoreLinearEdge","linear_edge.py",True),("LastCoreEdge","gui.py",False)]:
    a=Analysis([str(root / "edge-client" / entry)],
               pathex=[str(root / "edge-client"),str(root / "node-agent"),str(root / "node-agent/desktop")],
               binaries=[],datas=datas,hiddenimports=[],hookspath=[],hooksconfig={},runtime_hooks=[],
               excludes=["pytest","numpy","inference_engine","communication"],noarchive=False)
    # Linux keeps the host's ordinary X11/glibc libraries external, rather than
    # silently shipping another distro's libraries; retain Python and Tcl/Tk.
    if __import__('sys').platform.startswith('linux'):
        a.exclude_system_libraries(['*libpython*','*libtcl*','*libtk*','*libssl*','*libcrypto*'])
    exe=EXE(PYZ(a.pure),a.scripts,[],exclude_binaries=True,name=name,debug=False,
            strip=False,upx=False,console=console,disable_windowed_traceback=False)
    analyses.append(a);executables.append(exe)
collection=COLLECT(*executables,*[item for a in analyses for item in (a.binaries,a.datas)],
                   strip=False,upx=False,name="LastCoreEdge")
