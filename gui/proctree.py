"""Suspend, resume and kill a process together with everything it launched
(the pipeline's ffmpeg calls run as child processes of the worker).

Windows only, via ntdll's NtSuspendProcess/NtResumeProcess — the same calls
Task Manager-style tools use to "pause" a process. A suspended process uses
no CPU but keeps its memory, so resuming carries on exactly where it stopped.
"""

import ctypes
import subprocess
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_nt = ctypes.WinDLL("ntdll")

PROCESS_SUSPEND_RESUME = 0x0800
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
CREATE_NO_WINDOW = 0x08000000

# console hosts are spawned alongside console programs; they use no CPU, and
# freezing them gains nothing
_SKIP = {"conhost.exe"}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_nt.NtSuspendProcess.argtypes = [wintypes.HANDLE]
_nt.NtSuspendProcess.restype = ctypes.c_long
_nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
_nt.NtResumeProcess.restype = ctypes.c_long


def _descendants(root: int) -> list[int]:
    """PIDs of every process below `root` (children, grandchildren, ...)."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    kids, names = {}, {}
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
            names[e.th32ProcessID] = e.szExeFile.lower()
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    out, todo = [], list(kids.get(root, []))
    while todo:
        pid = todo.pop()
        if pid in out or pid == root:
            continue
        if names.get(pid) not in _SKIP:
            out.append(pid)
        todo.extend(kids.get(pid, []))
    return out


def _call(pid: int, fn) -> bool:
    h = _k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False  # already exited
    try:
        return fn(h) == 0
    finally:
        _k32.CloseHandle(h)


def suspend_tree(root: int) -> list[int]:
    """Suspends `root` first, so it can't start new children, then everything
    below it. Returns the PIDs actually suspended, for resume_tree()."""
    done = [root] if _call(root, _nt.NtSuspendProcess) else []
    for pid in _descendants(root):
        if _call(pid, _nt.NtSuspendProcess):
            done.append(pid)
    return done


def resume_tree(pids: list[int]):
    for pid in reversed(pids):  # children first, then the worker itself
        _call(pid, _nt.NtResumeProcess)


def kill_tree(root: int):
    """Terminates `root` and all its descendants (works while suspended)."""
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(root)],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


# -- job object: the worker dies with the window ------------------------------

class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         ctypes.c_void_p, wintypes.DWORD]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def bind_to_this_process(process_handle: int):
    """Puts a just-started worker in a job that Windows terminates when this
    process exits for any reason, so a crashed or force-closed window can't
    leave an invisible worker (and its ffmpeg children) burning CPU.
    Best-effort: returns the job handle to keep alive, or None."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (_k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                         ctypes.byref(info), ctypes.sizeof(info))
            and _k32.AssignProcessToJobObject(job, process_handle)):
        _k32.CloseHandle(job)
        return None
    return job
