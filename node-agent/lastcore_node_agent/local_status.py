"""Optional local-only, allowlisted UI observations; never a recovery journal."""
from __future__ import annotations
import json
import math
import os
import re
from pathlib import Path
import tempfile
import threading
import time

# No tokens, URLs, hostnames, prompts, peer lists or provider error text.
TASK_FIELDS = {'task_id','assignment_id','model_id','executor','layer_start','layer_end',
               'status','error_code','cleanup_confirmed','report_status','result'}
RESULT_FIELDS = {'output_shape','output_preview','local_seconds','parameter_bytes'}
ERROR_CODES = {'unreachable','heartbeat-failed','heartbeat-stale','rejected','recovery-required',
    'poll-unavailable','poll-rejected','deadline','heartbeat-stall','execution_failed',
    'executor_unavailable','cleanup_timeout','invalid-command'}


def safe_task(value):
    if not isinstance(value,dict): return None
    task = {k:v for k,v in value.items() if k in TASK_FIELDS}
    for key in ['task_id','assignment_id','model_id','executor','status','error_code','report_status']:
        if key in task: task[key] = str(task[key])[:128] if task[key] is not None else None
    for key in ['layer_start','layer_end']:
        if key in task and (not isinstance(task[key],int) or isinstance(task[key],bool)): task[key] = None
    if 'cleanup_confirmed' in task: task['cleanup_confirmed'] = task['cleanup_confirmed'] is True
    if 'result' in task:
        result = task['result']
        result = {k:v for k,v in result.items() if k in RESULT_FIELDS} if isinstance(result,dict) else None
        if result:
            preview=result.get('output_preview')
            if not isinstance(preview,list) or len(preview)>8 or not all(isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n) for n in preview): result.pop('output_preview',None)
            shape = result.get('output_shape')
            if not isinstance(shape,list) or len(shape)>8 or not all(isinstance(n,int) and n>=0 for n in shape): result.pop('output_shape',None)
            for key in ['local_seconds','parameter_bytes']:
                n = result.get(key)
                if not isinstance(n,(int,float)) or isinstance(n,bool) or not math.isfinite(n) or n<0: result.pop(key,None)
        task['result'] = result
    return task


def safe_admission(value):
    """Only explicit booleans and matching acknowledgement identities are visible."""
    if not isinstance(value, dict) or type(value.get('desired')) is not bool:
        return None
    confirmed = value.get('confirmed')
    if confirmed is not None and type(confirmed) is not bool:
        return None
    identifiers = {}
    for key in ('request_id', 'confirmed_request_id'):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item)):
            return None
        identifiers[key] = item
    if value.get('status') not in ('pending', 'confirmed'):
        return None
    matched = confirmed is value['desired'] and identifiers['request_id'] == identifiers['confirmed_request_id']
    return {'desired': value['desired'], 'confirmed': confirmed, **identifiers,
            'status': 'confirmed' if value['status'] == 'confirmed' and matched else 'pending'}


class LocalStatusWriter:
    def __init__(self,path):
        self.path=Path(path); self.lock=threading.Lock()
        self.error_channels = {}
        self.value={'connection':'starting','last_heartbeat':None,'heartbeat_ttl':None,'task':None}

    def __call__(self,updates):
        with self.lock:
            if isinstance(updates.get('node_id'),str): self.value['node_id']=updates['node_id'][:128]
            for key in ['connection','poll_status']:
                if key in updates: self.value[key]=str(updates[key])[:32]
            for key in ['last_heartbeat','heartbeat_ttl']:
                value=updates.get(key)
                if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0: self.value[key]=value
            if 'admission' in updates: self.value['admission']=safe_admission(updates['admission'])
            if 'control_error' in updates: self.value['control_error']='invalid-command' if updates['control_error']=='invalid-command' else None
            if 'task' in updates: self.value['task']=safe_task(updates['task'])
            patch=safe_task(updates.get('task_update'))
            if patch and self.value['task'] and patch.get('assignment_id')==self.value['task'].get('assignment_id'):
                self.value['task'].update(patch)
            candidates = {}
            if 'connection' in updates: candidates['connection'] = updates['connection']
            if 'poll_status' in updates: candidates['poll'] = 'poll-' + str(updates['poll_status'])
            if 'control_error' in updates: candidates['control'] = updates['control_error']
            if 'task' in updates: candidates['task'] = (safe_task(updates['task']) or {}).get('error_code')
            if patch and patch.get('assignment_id') == (self.value.get('task') or {}).get('assignment_id') and 'error_code' in patch:
                candidates['task'] = patch['error_code']
            errors = self.value.setdefault('recent_errors', [])
            for channel, candidate in candidates.items():
                code = candidate if isinstance(candidate,str) and candidate in ERROR_CODES else None
                if code is not None and self.error_channels.get(channel) != code:
                    errors.append(code)
                self.error_channels[channel] = code
            self.value['recent_errors'] = errors[-8:]
            self.value['observed_at']=time.time()
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix='.ui-status-',dir=self.path.parent)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as out:
                    json.dump(self.value,out,allow_nan=False)
                os.chmod(name,0o600); os.replace(name,self.path)
            finally:
                Path(name).unlink(missing_ok=True)
