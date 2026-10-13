"""License collection includes nested notices and discovers dotted native modules."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

DESKTOP = Path(__file__).resolve().parents[1] / 'desktop'
spec = importlib.util.spec_from_file_location('desktop_licenses', DESKTOP / 'collect_licenses.py')
licenses = importlib.util.module_from_spec(spec)
spec.loader.exec_module(licenses)


def test_numpy_nested_nonstandard_license_name_is_included():
    dist = SimpleNamespace(files=[Path('numpy-2.dist-info/licenses/numpy/src/dragon4_LICENSE.txt'),
                                  Path('numpy-2.dist-info/licenses/LICENSE.txt'),
                                  Path('numpy-2.dist-info/METADATA')])
    assert licenses.license_files(dist) == ['numpy-2.dist-info/licenses/LICENSE.txt',
                                           'numpy-2.dist-info/licenses/numpy/src/dragon4_LICENSE.txt']


def test_frozen_native_submodule_resolves_to_top_level_distribution(tmp_path, monkeypatch):
    toc = tmp_path / 'app'
    toc.mkdir()
    (toc / 'Analysis-00.toc').write_text(repr([('numpy._core._multiarray_umath', '/numpy/ext.so', 'EXTENSION')]))
    monkeypatch.setattr(licenses.importlib_metadata, 'packages_distributions', lambda: {'numpy': ['NumPy']})
    assert licenses.frozen_distributions(tmp_path) == {'numpy'}
