"""Private, bounded JSON admission commands; no sockets, secrets, or executable text."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import threading

from .credentials import StateError, _atomic_write_json, _json_object

MAX_COMMAND_BYTES = 1024


class ControlError(ValueError):
    pass


def validate_request_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is None:
        raise ControlError('invalid request id')


def _validate_command(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'request_id', 'accepting_tasks'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['accepting_tasks']) is not bool):
        raise ControlError('invalid command schema')
    validate_request_id(value['request_id'])
    return value


def _windows_private_acl(path):
    """Reject allow ACEs outside owner/Administrators/SYSTEM using native Win32 ACLs."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    get_info = advapi.GetNamedSecurityInfoW
    get_info.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                         ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                         ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    get_info.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [pointer, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    user_string = wintypes.LPWSTR()
    descriptor = pointer()
    rendered = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ControlError('cannot verify control owner')
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ControlError('cannot verify control owner')
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise ControlError('cannot verify control owner')
        sid = ctypes.cast(user, ctypes.POINTER(pointer))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(user_string)):
            raise ControlError('cannot verify control owner')
        if get_info(str(path), 1, 5, None, None, None, None, ctypes.byref(descriptor)):
            raise ControlError('cannot verify private ACL')
        if not convert(descriptor, 1, 5, ctypes.byref(rendered), None):
            raise ControlError('cannot verify private ACL')
        text = rendered.value
        owner = re.search(r'O:(.*?)(?=G:|D:|S:|$)', text)
        aces = re.findall(r'\(([^()]*)\)', text)
        if owner is None or not aces:
            raise ControlError('non-private ACL')
        allowed = {user_string.value, 'BA', 'SY', 'S-1-5-32-544', 'S-1-5-18'}
        if owner.group(1) not in allowed:
            raise ControlError('control path belongs to another owner')
        for ace in aces:
            fields = ace.split(';')
            if len(fields) != 6 or fields[0] not in {'A', 'D'}:
                raise ControlError('unsupported private ACL')
            if fields[0] == 'A' and fields[5] not in allowed:
                raise ControlError('non-private ACL')
    finally:
        if user_string:
            kernel.LocalFree(ctypes.cast(user_string, pointer))
        if token:
            kernel.CloseHandle(token)
        if rendered:
            kernel.LocalFree(ctypes.cast(rendered, pointer))
        if descriptor:
            kernel.LocalFree(descriptor)


def _private_stat(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ControlError('control path is not a regular private path')
    if os.name == 'nt':
        _windows_private_acl(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ControlError('control path is not private')
    return info


def write_control_command(path, request_id, accepting_tasks):
    path = Path(path)
    command = _validate_command({'version': 1, 'request_id': request_id,
                                 'accepting_tasks': accepting_tasks})
    _private_stat(path.parent, directory=True)
    try:
        _private_stat(path)
    except FileNotFoundError:
        pass
    # Reuse credential journal fsync + chmod + atomic replace, without reading credentials.
    _atomic_write_json(path, command)
    _private_stat(path)


class LocalControlReader:
    def __init__(self, path):
        self.path = Path(path)
        self._seen = {}
        self._lock = threading.Lock()

    def consume(self, agent):
        with self._lock:
            _private_stat(self.path.parent, directory=True)
            try:
                before = _private_stat(self.path)
            except FileNotFoundError:
                return False
            if before.st_size > MAX_COMMAND_BYTES:
                raise ControlError('control command too large')
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
            fd = os.open(self.path, flags)
            with os.fdopen(fd, 'rb') as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ControlError('control file changed during open')
                data = handle.read(MAX_COMMAND_BYTES + 1)
            if len(data) > MAX_COMMAND_BYTES:
                raise ControlError('control command too large')
            try:
                command = _validate_command(json.loads(data.decode('utf-8'), object_pairs_hook=_json_object))
            except (ValueError, StateError, UnicodeError, RecursionError) as exc:
                raise ControlError('invalid control JSON') from exc
            request_id, desired = command['request_id'], command['accepting_tasks']
            if request_id in self._seen:
                if self._seen[request_id] != desired:
                    raise ControlError('request id reused with different command')
                return False
            try:
                agent.set_accepting_tasks(desired, request_id)
            except ValueError as exc:
                raise ControlError('control request conflicts with existing intent') from exc
            self._seen[request_id] = desired
            return True
