"""Unity 에디터의 알려진 모달 대화상자 자동 처리 (Windows 전용).

"Script Updating Consent" 모달은 에디터 메인 스레드를 멈춰 브리지를 마비시킨다.
GUI 모드에서 이 모달을 끄는 공식 방법이 없으므로(-accept-apiupdate는 배치 전용),
네이티브 Win32 대화상자를 감지해 동의 버튼을 직접 클릭한다.

창 제목과 버튼 텍스트를 정확히 매칭할 때만 동작하며, 못 찾으면 아무것도 하지
않는다 (수동 처리로 자연 강등).
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32

_BM_CLICK = 0x00F5
_SMTO_ABORTIFHUNG = 0x0002

_CONSENT_TITLE = "Script Updating Consent"
_CONSENT_BUTTON = "Yes, for these and other files that might be found later"

# 씬이 dirty라 SceneAutoReload(에디터 스크립트)가 개입하지 않을 때 뜨는 모달.
# "Ignore"는 현재 에디터 상태를 유지하는 선택이라 데이터 손실 없이 안전하다.
_SCENE_TITLE = "The open scene(s) have been modified externally"
_SCENE_BUTTON = "Ignore"


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _find_child_button(dialog: int, text: str) -> int:
    """대화상자에서 주어진 텍스트를 가진 자식 컨트롤 핸들. 없으면 0."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _):
        if _window_text(hwnd) == text:
            found.append(hwnd)
            return False
        return True

    _user32.EnumChildWindows(dialog, enum_cb, 0)
    return found[0] if found else 0


def _click_dialog_button(title: str, button_text: str) -> bool:
    """주어진 제목의 대화상자에서 정확히 일치하는 버튼을 클릭. 성공 시 True."""
    dialog = _user32.FindWindowW(None, title)
    if not dialog:
        return False
    button = _find_child_button(dialog, button_text)
    if not button:
        return False
    # SendMessageTimeout: 대화상자 스레드가 멈춰 있어도 호스트가 같이 안 멈추게
    result = wintypes.DWORD()
    _user32.SendMessageTimeoutW(
        button, _BM_CLICK, 0, 0, _SMTO_ABORTIFHUNG, 2000, ctypes.byref(result)
    )
    return True


def dismiss_script_update_consent() -> bool:
    """"Script Updating Consent" 모달이 떠 있으면 전체 동의 버튼을 클릭."""
    return _click_dialog_button(_CONSENT_TITLE, _CONSENT_BUTTON)


def dismiss_scene_modified_ignore() -> bool:
    """"씬이 외부에서 수정됨" 모달이 떠 있으면 Ignore(현재 상태 유지)를 클릭.

    깨끗한(clean) 씬은 SceneAutoReload가 먼저 자동 리로드하므로 이 모달 자체가
    안 뜬다. 여기 오는 것은 dirty 씬뿐이고, Ignore는 작업을 보존하는 선택이다.
    """
    return _click_dialog_button(_SCENE_TITLE, _SCENE_BUTTON)
