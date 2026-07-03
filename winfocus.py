"""Unity 에디터 창 포커스 유틸 (Windows 전용).

Unity는 백그라운드에 있으면 스크립트 컴파일/도메인 리로드를 미루므로,
컴파일이 필요한 시점에 잠깐 포커스를 줬다가 원래 창으로 되돌린다.
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _image_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def find_unity_hwnd() -> int:
    """보이는 창 중 Unity.exe의 메인 창 핸들. 없으면 0."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _image_name(pid.value).lower().endswith("\\unity.exe"):
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(enum_cb, 0)
    return found[0] if found else 0


def focus_unity() -> int:
    """Unity 창에 포커스. 이전 포그라운드 창 핸들을 반환 (복원용, 실패 시 0)."""
    hwnd = find_unity_hwnd()
    if not hwnd:
        return 0
    prev = _user32.GetForegroundWindow()
    _user32.SwitchToThisWindow(hwnd, True)
    return prev if prev and prev != hwnd else 0


def restore_focus(prev: int) -> None:
    if prev:
        _user32.SwitchToThisWindow(prev, True)
