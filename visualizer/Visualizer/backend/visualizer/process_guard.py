"""Windows job isolation for app-owned large-file feature workers.

The worker assigns itself before importing the predictor or starting a child.
Windows closes its noninheritable job handle if the worker exits or is killed,
terminating descendants too. The aggregate memory budget includes native children.
"""
import os


def enter_resource_job(memory_bytes):
    if os.name != 'nt':
        raise ValueError('Large-file extraction requires Windows job memory and process-tree isolation')
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Could not create large-file resource job')
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
    limits.JobMemoryLimit = memory_bytes
    if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
            not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error, 'Could not apply large-file process-tree/memory isolation')
    # Retain this kernel handle until process teardown. Closing it explicitly
    # while assigned would kill this worker before its result can be delivered.
    return handle
