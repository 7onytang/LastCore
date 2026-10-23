"""Versioned admission policy; never changes a saved request or starts an executor."""
import json
import math
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class PolicyError(HTTPException):
    def __init__(self, status_code, detail, error_code):
        super().__init__(status_code, detail)
        self.error_code = error_code


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    model_id: str = Field(min_length=1)
    family: Literal['linear', 'pi', 'log_stats', 'whole_model', 'local_stage']
    version: str = Field(min_length=1)
    executable: StrictBool
    source: Literal['bundled_algorithm', 'pinned_gguf', 'unqualified_local_weights']
    artifact_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    backend_commit: str | None = Field(default=None, pattern=r'^[a-f0-9]{40}$')
    limits: dict[str, StrictInt] | None
    memory_policy: Literal['declared_admission_only', 'unavailable']
    # Unknown is explicit: these profiles do not claim a hard RSS cap or measurements.
    native_hard_memory_limit_mb: None = None
    measured_peak_memory_mb: None = None
    blocker: str | None = None

    @model_validator(mode='after')
    def complete(self):
        common = {'num_layers', 'layer_memory_mb', 'max_nodes', 'timeout_seconds'}
        required = {'linear': common | {'input_values', 'input_abs'},
                    'pi': common | {'total_samples'},
                    'log_stats': common | {'log_lines', 'line_characters', 'total_characters'},
                    'whole_model': {'prompt_bytes', 'max_tokens', 'timeout_seconds',
                                    'execution_timeout_seconds', 'output_bytes'}}
        if self.executable:
            if (self.family not in required or self.limits is None
                    or set(self.limits) != required[self.family]
                    or any(value <= 0 for value in self.limits.values())
                    or self.memory_policy != 'declared_admission_only' or self.blocker is not None):
                raise ValueError('executable profile lacks required budget metadata')
            if self.family == 'whole_model' and (self.source != 'pinned_gguf'
                    or not self.artifact_sha256 or not self.backend_commit):
                raise ValueError('whole-model profile lacks pinned artifact metadata')
            arithmetic = {'linear': 'lastcore/linear-demo-v1', 'pi': 'lastcore/pi-monte-carlo-v1',
                          'log_stats': 'lastcore/log-statistics-v1'}
            if self.family in arithmetic and (self.model_id != arithmetic[self.family] or self.version != 'v1'):
                raise ValueError('profile has no matching versioned builtin implementation')
            if self.family != 'whole_model' and (self.source != 'bundled_algorithm'
                    or self.artifact_sha256 is not None or self.backend_commit is not None):
                raise ValueError('arithmetic profile cannot select a downloadable artifact')
        elif not self.blocker:
            raise ValueError('unavailable profile must explain missing metadata')
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    policy_version: Literal['lastcore-task-policy-v1']
    profiles: list[Profile] = Field(min_length=1)


class TaskPolicy:
    def __init__(self, legacy_model_ids=False, manifest=None):
        if type(legacy_model_ids) is not bool:
            raise ValueError('legacy model compatibility must be a boolean')
        data = manifest if manifest is not None else json.loads(
            Path(__file__).with_name('task-policy-v1.json').read_text(encoding='utf-8'))
        parsed = Manifest.model_validate(data)
        self.profiles = {profile.model_id: profile for profile in parsed.profiles}
        if len(self.profiles) != len(parsed.profiles):
            raise ValueError('duplicate model profiles are forbidden')
        from .inference_jobs import MODEL_ID, MODEL_REVISION, MODEL_SHA256, BACKEND_COMMIT, RESULT_OUTPUT_BYTES
        for profile in self.profiles.values():
            if profile.executable and profile.family == 'whole_model' and (
                    profile.model_id != MODEL_ID or profile.version != MODEL_REVISION
                    or profile.artifact_sha256 != MODEL_SHA256
                    or profile.backend_commit != BACKEND_COMMIT):
                raise ValueError('profile differs from the actual whole-model backend contract')
            if (profile.executable and profile.family == 'whole_model'
                    and profile.limits['output_bytes'] != RESULT_OUTPUT_BYTES):
                raise ValueError('v1 output budget must match the enforced result output limit')
        self.legacy_model_ids = legacy_model_ids

    def admit(self, request, family):
        profile = self.profiles.get(request.model_id)
        if self.legacy_model_ids and family == 'linear' and (profile is None or profile.family in ('local_stage', 'whole_model')):
            return  # Explicit legacy simulation/stage compatibility, never strict acceptance.
        if profile is None:
            raise PolicyError(422, 'model is not permitted by task policy', 'MODEL_NOT_ALLOWED')
        if not profile.executable:
            raise PolicyError(422, 'model profile lacks executable metadata', 'MODEL_METADATA_UNAVAILABLE')
        if profile.family != family:
            raise PolicyError(422, 'model does not match the execution family', 'MODEL_NOT_ALLOWED')
        limits = profile.limits
        for field in ('num_layers', 'layer_memory_mb', 'max_nodes', 'timeout_seconds',
                      'total_samples', 'max_tokens', 'execution_timeout_seconds'):
            value = getattr(request, field, None)
            if value is not None and field in limits and value > limits[field]:
                raise PolicyError(422, 'task exceeds the profile budget', 'BUDGET_REJECTED')
        if family == 'whole_model' and len(request.prompt.encode('utf-8')) > limits['prompt_bytes']:
            raise PolicyError(422, 'task exceeds the profile budget', 'BUDGET_REJECTED')
        if family == 'linear' and (len(request.input_vector) > limits['input_values']
                or any(abs(value) > limits['input_abs'] for value in request.input_vector)):
            raise PolicyError(422, 'task exceeds the profile budget', 'BUDGET_REJECTED')
        if family == 'log_stats' and (len(request.log_lines) > limits['log_lines']
                or any(len(line) > limits['line_characters'] for line in request.log_lines)
                or sum(map(len, request.log_lines)) > limits['total_characters']):
            raise PolicyError(422, 'task exceeds the profile budget', 'BUDGET_REJECTED')


def validation_code(errors, body, policy=None):
    """Classify schema failures without parsing mutable English validation messages."""
    extras = {str(error['loc'][-1]) for error in errors if error['type'] == 'extra_forbidden'}
    supplied = set(body) if isinstance(body, dict) else set()
    prohibited = extras | supplied
    if prohibited & {'command', 'shell', 'code', 'script', 'executor', 'executable'}:
        return 'EXECUTABLE_FIELD_FORBIDDEN'
    if prohibited & {'model_url', 'model_path', 'backend_url', 'source_url', 'artifact_url'}:
        return 'MODEL_SOURCE_UNTRUSTED'
    if prohibited & {'input_ref', 'output_ref'}:
        return 'REFERENCE_UNSUPPORTED'
    if extras:
        return 'REQUEST_INVALID'
    if isinstance(body, dict):
        if 'workload' in body and body['workload'] not in ('linear', 'pi', 'log_stats'):
            return 'TASK_TYPE_UNSUPPORTED'
        known_families = {'lastcore/linear-demo-v1': 'linear', 'lastcore/pi-monte-carlo-v1': 'pi',
                          'lastcore/log-statistics-v1': 'log_stats'}
        model = body.get('model_id')
        if isinstance(model, str) and model in known_families and body.get('workload', 'linear') != known_families[model]:
            return 'MODEL_NOT_ALLOWED'
        if body.get('workload') in ('pi', 'log_stats'):
            known = {'pi': 'lastcore/pi-monte-carlo-v1', 'log_stats': 'lastcore/log-statistics-v1'}
            if body.get('model_id') != known[body['workload']]:
                return 'MODEL_NOT_ALLOWED'
    if policy is not None and isinstance(body, dict) and isinstance(body.get('model_id'), str):
        profile = policy.profiles.get(body['model_id'])
        if profile and profile.executable:
            for field in ('num_layers', 'max_nodes', 'timeout_seconds', 'total_samples'):
                value = body.get(field)
                if type(value) is int and field in profile.limits and value > profile.limits[field]:
                    return 'BUDGET_REJECTED'
        if (body.get('workload') == 'pi' and type(body.get('num_layers')) is int
                and type(body.get('total_samples')) is int and body['num_layers'] > body['total_samples']):
            return 'BUDGET_REJECTED'
    if any(error['loc'][-1] in ('model_sha256', 'backend_commit', 'backend_sha256') for error in errors):
        return 'MODEL_SOURCE_UNTRUSTED'
    if any(error['loc'][-1] == 'model_id' for error in errors):
        return 'MODEL_NOT_ALLOWED'
    budgets = {'num_layers', 'layer_memory_mb', 'max_nodes', 'timeout_seconds', 'total_samples',
               'max_tokens', 'execution_timeout_seconds', 'prompt', 'input_vector', 'log_lines'}
    if any(any(field in budgets for field in error['loc']) for error in errors):
        return 'BUDGET_REJECTED'
    return 'REQUEST_INVALID'


def finite_error_values(value):
    """Non-finite invalid inputs must not make FastAPI's JSON rejection itself fail."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: finite_error_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(finite_error_values(item) for item in value)
    return value
