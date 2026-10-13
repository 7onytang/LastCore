from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'desktop'))
import resource_snapshot


def test_no_sampler_reports_unknown_not_zero():
    sampler=resource_snapshot.ResourceSampler();sampler.psutil=None
    data=sampler.sample()
    assert data['cpu_percent'] is None
    assert data['memory_available_bytes'] is None
    assert data['accelerator'] is None


def test_cpu_needs_a_real_delta_and_memory_is_measured(monkeypatch):
    from collections import namedtuple
    Counters=namedtuple('Counters','user system idle')
    samples=iter([Counters(10,10,80),Counters(15,10,85)])
    sampler=resource_snapshot.ResourceSampler()
    sampler.psutil=SimpleNamespace(cpu_times=lambda:next(samples),
        virtual_memory=lambda:SimpleNamespace(available=512,total=1024))
    ticks=iter([0,1]);monkeypatch.setattr(resource_snapshot.time,'monotonic',lambda:next(ticks))
    assert sampler.sample()['cpu_percent'] is None
    value=sampler.sample()
    assert value['cpu_percent']==50
    assert value['memory_available_bytes']==512 and value['memory_total_bytes']==1024


def test_sensor_failure_leaves_unknown_without_breaking_other_fields():
    sampler=resource_snapshot.ResourceSampler()
    def unavailable(): raise RuntimeError('simulated unsupported sensor')
    sampler.psutil=SimpleNamespace(cpu_times=unavailable,virtual_memory=unavailable)
    value=sampler.sample()
    assert value['cpu_percent'] is None and value['memory_total_bytes'] is None
    assert value['sampled_at']>0
