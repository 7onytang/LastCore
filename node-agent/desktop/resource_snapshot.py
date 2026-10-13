"""Read aggregate OS counters only. Missing sensors stay unknown, never zero."""
import os
import threading
import time

class ResourceSampler:
    def __init__(self):
        self.lock=threading.Lock(); self.previous=None; self.percent=None
        try:
            import psutil
            self.psutil=psutil
        except ImportError:
            self.psutil=None

    def sample(self):
        value={'sampled_at':time.time(),'cpu_percent':None,'cpu_logical':os.cpu_count(),
               'memory_available_bytes':None,'memory_total_bytes':None,'accelerator':None}
        if self.psutil is None: return value
        with self.lock:
            try:
                counters=self.psutil.cpu_times()
                total=sum(counters)-getattr(counters,'guest',0)-getattr(counters,'guest_nice',0)
                idle=counters.idle+getattr(counters,'iowait',0)
                now=time.monotonic()
                if self.previous and now-self.previous[2]>=.2:
                    dt,di=total-self.previous[0],idle-self.previous[1]
                    self.percent=round(max(0,min(100,(dt-di)/dt*100)),1) if dt>0 else None
                    self.previous=(total,idle,now)
                elif self.previous is None: self.previous=(total,idle,now)
                value['cpu_percent']=self.percent
            except Exception: pass
            try:
                memory=self.psutil.virtual_memory()
                value['memory_available_bytes']=memory.available
                value['memory_total_bytes']=memory.total
            except Exception: pass
        return value
