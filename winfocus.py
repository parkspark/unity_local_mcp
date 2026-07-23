"""Unity 에디터 창 포커스 유틸 (Windows 전용).

Unity는 백그라운드에 있으면 스크립트 컴파일/도메인 리로드를 미루므로,
컴파일이 필요한 시점에 잠깐 포커스를 줬다가 원래 창으로 되돌린다.
"""

import ctypes
import os
import time
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


def find_unity_hwnd(project_dir: str | None = None) -> int:
    """Find the visible Unity main window for ``project_dir``.

    Unity window titles contain the project name. If multiple editors are open
    and none matches, fail closed instead of focusing an unrelated project.
    """
    found: list[tuple[int, str]] = []

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
            title = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, title, len(title))
            found.append((hwnd, title.value))
        return True

    _user32.EnumWindows(enum_cb, 0)
    if not found:
        return 0
    if project_dir:
        project_name = os.path.basename(os.path.normpath(project_dir)).casefold()
        matched = [hwnd for hwnd, title in found if project_name in title.casefold()]
        if len(matched) == 1:
            return matched[0]
        if len(found) > 1:
            return 0
    return found[0][0]


def focus_unity(project_dir: str | None = None, retries: int = 3) -> int:
    """Focus the target Unity window and verify that Windows accepted it."""
    hwnd = find_unity_hwnd(project_dir)
    if not hwnd:
        return 0
    prev = _user32.GetForegroundWindow()
    if prev == hwnd:
        return 0
    for _ in range(max(1, retries)):
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Windows normally prevents a background process from stealing focus.
        # Temporarily join the foreground/target input queues and synthesize an
        # ALT transition, then verify the result instead of trusting the API.
        foreground = _user32.GetForegroundWindow()
        foreground_pid = wintypes.DWORD()
        target_pid = wintypes.DWORD()
        foreground_thread = (
            _user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
            if foreground else 0
        )
        target_thread = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        current_thread = _kernel32.GetCurrentThreadId()
        attached_foreground = bool(
            foreground_thread and foreground_thread != current_thread
            and _user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        attached_target = bool(
            target_thread and target_thread != current_thread
            and _user32.AttachThreadInput(current_thread, target_thread, True)
        )
        _user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
        _user32.keybd_event(0x12, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        _user32.SwitchToThisWindow(hwnd, True)
        if attached_target:
            _user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground:
            _user32.AttachThreadInput(current_thread, foreground_thread, False)
        for _ in range(5):
            if _user32.GetForegroundWindow() == hwnd:
                return prev if prev else 0
            time.sleep(0.05)
    # A non-zero previous handle would falsely imply success to callers.
    return 0


def restore_focus(prev: int) -> None:
    if prev:
        _user32.SwitchToThisWindow(prev, True)
