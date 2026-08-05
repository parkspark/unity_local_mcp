"""v1.9 host-owned Unity verification specifications, evidence and receipts.

The builder model may propose work, but it cannot mark that work complete.  A
host-selected read/play/input sequence turns real Unity JSON results into
deterministic pass/fail evidence.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable

import config
from preflight import inspect_request
from policy_lint import lint_scripts
from version import __version__

VERSION = __version__
_BUILD_WORDS = (
    "만들", "제작", "구현", "생성", "수정", "개선", "업데이트", "추가", "삭제",
    # Common Korean repair verbs. Without these a behaviour bug report like
    # "부스트가 동작하지 않는다. 고쳐줘" skipped host verification entirely and
    # left the model free to declare success on its own.
    "고치", "고쳐", "해결",
    "build", "create", "make", "implement", "update", "fix", "add", "remove",
    "repair",
)
_GAME_WORDS = ("게임", "플랫포머", "횡스크롤", "platformer", "side-scroller", "game")
# Behaviour verbs are strong signals on their own. "Space 점프가 동작하지 않는다.
# 수정하고 검증한다" must enable jump measurement even though it never says 게임 —
# gating these on a game keyword is what allowed an empty spec to report success.
_MOVEMENT_WORDS = ("이동", "움직", "걷", "달리", "movement", "walking", "running")
_MOVEMENT_CONTEXT_WORDS = ("플랫포머", "횡스크롤", "플레이어", "player")
_JUMP_WORDS = ("점프", "jump", "jumping", "착지", "landing")
_JUMP_CONTEXT_WORDS = ("플랫포머", "platformer")
_CAMERA_WORDS = ("카메라", "camera")
# "추적"이 빠져 있어 "카메라를 추가해서 플레이어를 추적하게해"가 카메라 검사를
# 하나도 만들지 못했다. 사용자가 실제로 쓰는 낱말이다.
_CAMERA_FOLLOW_WORDS = ("추종", "추적", "따라", "쫓", "follow", "following", "track", "tracking")
_LEVEL_WORDS = ("levelloader", "level json", "레벨 json", "데이터 주도", "data-driven")
_BOOST_WORDS = ("부스트", "boost", "dash", "대시")
# 한글 표기가 빠져 있어 "좌쉬프트키를 누르면 순간적으로 가속"이 부스트로 잡히지
# 않았다. 호스트 부스트 측정은 이동키와 leftShift를 함께 누르므로 이 요청에 맞다.
_BOOST_CONTEXT_WORDS = ("shift", "쉬프트", "시프트", "쉬프트키", "시프트키")
_LANDING_WORDS = ("착지", "land", "landing")
# Control scheme named in the request. "A/D 좌우 이동" must be verified with A/D,
# not with the harness's default arrow keys.
# 구분자 없는 "ad키"/"AD 키"는 사용자가 가장 흔히 쓰는 표기인데 빠져 있었다. 놓치면
# 하네스가 방향키로 측정하고, A/D로 올바르게 구현한 게임을 떨어뜨린다 — v1.11.4가
# 고친 것과 같은 계열의 결함이다.
# 경계를 `\b`로 잡으면 안 된다 — 한글은 단어 문자라서 "D로"의 d와 로 사이에 경계가
# 없고, "A, D로 이동"이 통째로 빠진다. _has_word와 같은 이유로 라틴 문자에 대해서만
# 경계를 정의한다. 조사는 무엇이든("ad키", "ad로", "AD") 같은 조작 체계이므로 낱말
# 자체로 인식한다 — 조사마다 패턴을 늘리다 "ad로"를 또 놓쳤다.
_AD_SCHEME = re.compile(
    r"(?<![a-z0-9])a\s*[/,·+]\s*d(?![a-z0-9])|"
    r"(?<![a-z0-9])d\s*[/,·+]\s*a(?![a-z0-9])|"
    r"(?<![a-z0-9])wasd(?![a-z0-9])|"
    r"(?<![a-z0-9])ad(?![a-z0-9])|"
    r"(?<![a-z0-9])da\s*(?:키|key)|"
    r"(?<![a-z0-9])a\s*(?:와|and)\s*d(?![a-z0-9])",
    re.I,
)
# 씬을 새로 만들라는 요청의 표기. "새씬"처럼 붙여 쓰는 형태가 빠져 있었다.
FRESH_SCENE_PHRASES = (
    "새 빈 씬", "새빈씬", "새 씬", "새씬", "빈 씬", "빈씬", "새로운 씬", "새로운씬",
    "new empty scene", "new scene",
)
# 좌우 양방향을 뜻하는 표현. "ad키"에는 \ba\b도 \bd\b도 없어서 기존 판정이 양방향
# 검사를 통째로 빠뜨렸다.
_BIDIRECTIONAL_WORDS = ("좌우", "양쪽", "양방향", "left and right", "both directions")
# 관찰 카메라와 1인칭 시점을 가르는 최소 거리. 기본 캡슐이 2유닛이므로 그 안쪽이면
# 플레이어를 화면에 담을 수 없다. 2.5D 측면 카메라는 보통 8~12유닛 뒤에 있다.
_CAMERA_MIN_GAP = 1.5
_ARROW_SCHEME = re.compile(r"방향키|화살표|arrow\s*keys?", re.I)
# Jump key named in the request. Same class of bug as the movement scheme:
# testing space against a game bound to W fails it for the harness's assumption.
_JUMP_KEY_SCHEMES = (
    (re.compile(r"\bw\s*(?:키|key)?\s*(?:로|으로)?\s*(?:점프|jump)", re.I), "w"),
    (re.compile(r"(?:위쪽\s*방향키|up\s*arrow|uparrow)\s*(?:키|key)?\s*(?:로|으로)?\s*(?:점프|jump)", re.I), "upArrow"),
)
# The single jump implementation this harness can actually measure. The bridge
# re-queues a held key every editor tick, so wasPressedThisFrame can be gone
# before gameplay Update runs; but a plain isPressed latch re-arms every frame
# and stacks impulses. Both failure modes are fixed by an explicit rising edge.
_JUMP_EDGE_GUIDANCE = (
    "점프 입력은 Update에서 bool pressed = Keyboard.current.spaceKey.isPressed로 "
    "읽고, 직전 프레임 상태를 담은 필드(예: jumpHeld)와 비교해 pressed && !jumpHeld일 "
    "때만 jumpRequested = true로 세운 뒤 마지막에 jumpHeld = pressed로 갱신한다. "
    "FixedUpdate에서는 jumpRequested && isGrounded일 때 impulse를 한 번 적용하고 "
    "즉시 jumpRequested = false로 소비한다. wasPressedThisFrame 엣지에만 의존하지 "
    "말고, !jumpHeld 없이 isPressed만 보고 래치하지도 않는다."
)
# Runtime-behaviour language that demands Play Mode proof. If a request uses any
# of these but no concrete check could be derived, the host refuses to report
# success instead of silently verifying nothing (verification_spec_empty).
_BEHAVIOUR_HINT_WORDS = (
    "점프", "jump", "이동", "움직", "걷", "달리", "착지", "land", "landing",
    "부스트", "boost", "대시", "dash", "추종", "follow", "조작", "입력",
    "플레이", "play", "동작", "작동", "실행", "물리", "충돌", "collision",
    "gameplay", "runtime",
)
# 씬에 실체가 남아야 하는 생성 요청. "댕댕이 모양의 모델링 생성해줘"는 런타임 어휘도
# 검증 어휘도 없어 두 empty-spec 가드를 모두 비껴갔고, requested_checks 0개로
# `verified`가 나갔다(영수증 20260801_202621_246_08b6891343). 컴파일 0건과 씬 저장만
# 보고 통과시킨 것이라, 개가 만들어졌는지는 아무도 보지 않았다. 이 두 어휘가 겹치면
# 호스트가 계층을 읽어 실제로 오브젝트가 생겼는지 잰다(scene_objects).
_CREATE_WORDS = (
    "만들", "생성", "제작", "구현", "배치", "추가", "세우",
    "create", "make", "build", "spawn", "place", "add",
)
_SCENE_OBJECT_WORDS = (
    "오브젝트", "게임오브젝트", "모델링", "모델", "캐릭터", "프리팹",
    "큐브", "정육면체", "구체", "캡슐", "평면", "바닥", "지형", "맵",
    "스테이지", "레벨", "씬", "장면", "플레이어", "블록", "발판", "플랫폼",
    "object", "gameobject", "model", "modeling", "character", "prefab",
    "cube", "sphere", "capsule", "plane", "terrain", "scene", "player",
    "block", "platform",
)
# 스스로 움직이는 것을 요구하는 문구. 순찰하는 적, 자동으로 도는 발판처럼 입력 없이
# 움직여야 하는 대상이다. 2026-08-03 실행에서 "적을 좌우로 자동 순찰하게"가
# `verified`로 나갔고 — EnemyPatrol.cs는 실제로 만들어졌지만 — 그것이 움직이는지
# 본 검사는 하나도 없었다(영수증 20260803_002929 계열).
_AUTONOMOUS_WORDS = (
    "순찰", "patrol", "자동으로 움직", "자동 이동", "스스로 움직", "혼자 움직",
    "왔다갔다", "왔다 갔다", "자동으로 좌우", "자동 순찰",
)
# 한쪽 이동이 반대쪽의 이 비율에 못 미치면 그 방향이 막힌 것으로 본다. 기록된
# 119건의 분포에서 정상은 0.96 이상, 막힌 것은 0.42 이하이고 그 사이가 비어 있다.
_BLOCKED_PATH_RATIO = 0.5
# 새 씬이 기본으로 갖는 오브젝트. 이것만 남아 있으면 아무것도 만들어지지 않았다.
_DEFAULT_SCENE_OBJECTS = frozenset({
    "main camera", "directional light", "global volume",
})
# An explicit demand to verify. This is wider than the behaviour words above: a
# request can ask for proof without naming any behaviour this module knows how
# to measure. "10x20 격자 보드를 코드로 생성 ... 실제로 생성되는지 검증까지 끝내줘"
# extracted zero checks and the receipt still said `verified` after 40 seconds
# without ever entering Play Mode — the empty-set success this project has
# refused since v1.11.2, reached through a request shape the guard did not cover.
_VERIFICATION_REQUEST_WORDS = ("검증", "verify", "검사")
_VERIFICATION_REQUEST_PHRASES = re.compile(
    r"실제로\s*\S{0,12}?\s*(?:되는지|동작|작동|움직|생성)|"
    r"(?:되는지|동작|작동)\s*(?:하는지\s*)?(?:확인|테스트|체크)|"
    r"actually\s+(?:works?|moves?|jumps?|spawns?)",
    re.I,
)

# Requirements this module can actually turn into a measurement. A clause that
# names none of these is something the request asked for and the host will never
# look at.
_COVERED_CONCEPT_WORDS = (
    _MOVEMENT_WORDS + _JUMP_WORDS + _CAMERA_WORDS + _CAMERA_FOLLOW_WORDS
    + _BOOST_WORDS + _LEVEL_WORDS
    + ("좌우", "방향키", "키보드", "keyboard", "입력", "input", "카메라", "camera")
)
# Conditional and outcome language. A request sentence carrying one of these
# promises behaviour beyond locomotion: something is gained, lost, destroyed,
# counted or switched. The check vocabulary has no member for any of it, so the
# sentence would be silently dropped while `movement` alone reports `verified`.
_REQUIREMENT_MARKERS = re.compile(
    r"에\s*닿으면|에\s*맞으면|충돌하면|부딪히면|먹으면|"
    r"하면\s*\S*\s*(?:증가|감소|올라|내려|생기|사라|없어|바뀌|변경|표시)|"
    r"할\s*때\s*\S*\s*(?:증가|감소|올라|내려|생기|사라|없어|바뀌|변경|표시)|"
    r"획득|점수|스코어|score|체력|hp|목숨|life|lives|데미지|damage|"
    r"사라지|없어지|제거되|파괴되|destroy|despawn|"
    r"클리어|clear|게임\s*오버|game\s*over|승리|패배|"
    # `보드`는 "키보드" 안에서, `board`는 "Keyboard.current" 안에서 걸린다.
    # 구현 지시문("키보드 null 체크")이 요구 조항으로 잡혀 있었다.
    r"소환|스폰|spawn|생성하는|개\s*생성|격자|(?<!키)보드|grid|(?<!key)board|"
    # 낙사 복귀. "떨어지면 시작 위치로 되돌아오게"는 조건 표현이 있는데도 결과
    # 어휘(되돌아·복귀·리스폰)가 목록에 없어 조용히 빠졌다. policy_lint의
    # fall_respawn_check_missing은 "낙사 시 시작 위치로 복귀" 리터럴에만 걸린다.
    r"되돌아|복귀|리스폰|respawn|시작\s*위치로|"
    # 구조·개수 요구. "3층짜리 플랫폼을 만들어"는 조건 표현이 없어 놓쳤는데,
    # 층수가 맞는지 보는 검사는 어휘에 없다.
    r"\d+\s*(?:층|개|칸|줄|단)(?:짜리|의)?|층짜리|multi[- ]?level|"
    r"순찰|추적하는\s*적|자동으로\s*움직|patrol|"
    r"on\s+collision|when\s+.*\s+(?:hits?|touches?|collides?)",
    re.I,
)
# Sentence boundaries must not fall inside "Assets/Scenes/A.unity": a period
# only ends a sentence when whitespace or the end of the request follows it.
_SENTENCE_SPLIT = re.compile(r"(?:\.(?=\s)|\.$|[。!?\n])+")
# A sentence usually bundles what IS measured with what is not — "Player가 A/D로
# 이동하고 코인에 닿으면 점수가 1 올라가게". Judging the whole sentence lets the
# locomotion half vouch for the half nothing will look at, which is the exact
# failure this detector exists to find, so conjunctions are split first.
_CLAUSE_SPLIT = re.compile(r"하고\s+|해서\s+|그리고\s+|,\s*|\s+및\s+|면서\s+")
# Asset paths carry words the markers match by accident — the scene name
# `GridProceduralRepro1.unity` contains "Grid" and flagged the sentence that
# merely creates the scene. Paths are never a requirement clause.
_ASSET_TOKEN = re.compile(r"\bAssets/\S+", re.I)


# 검사가 실제로 덮는 문구. 제거된 거부권과 다른 점은 **어휘 가족이 아니라 검사
# 이름에 묶인다**는 것이다 — `좌우`가 이동 어휘라는 이유로 적의 순찰을 묵살하던
# 것이 그 결함이었다. 여기서는 `autonomous_motion`이 켜졌을 때만, 그 검사가 재는
# 문구만 지운다. 지운 뒤에도 마커가 남으면 그 절은 여전히 미매핑이다.
_CHECK_COVERAGE: dict[str, re.Pattern] = {
    "autonomous_motion": re.compile(
        r"순찰|patrol|자동\s*으?로?\s*움직|자동\s*이동|스스로\s*움직|혼자\s*움직|"
        r"왔다\s*갔다|자동\s*순찰",
        re.I,
    ),
}


def _unmapped_requirements(request: str, checks: Iterable[str]) -> list[str]:
    """Name request clauses that no requested check will ever measure.

    v1.11.13 refused runs whose check set was *empty*. This covers the harder
    case: the score request above extracts `movement` and reports `verified`
    without ever looking at the score — a subset reported as the whole.
    Recording only; the verdict is unchanged until false positives are measured
    on real prompts.
    """
    behaviour = [name for name in checks if not name.startswith("components:")]
    found: list[str] = []
    seen: set[str] = set()
    text = _ASSET_TOKEN.sub(" ", request or "")
    for sentence in _SENTENCE_SPLIT.split(text):
        for raw in _CLAUSE_SPLIT.split(sentence):
            clause = " ".join(raw.split())
            if len(clause) < 6 or not _REQUIREMENT_MARKERS.search(clause):
                continue
            # 켜져 있는 검사가 덮는 문구를 지운 뒤 다시 본다. 남는 마커가 없으면
            # 그 절은 측정되고 있는 것이므로 ⚠로 띄우지 않는다 — 재는데도 계속
            # 경고하면 이 줄 전체가 무시당한다.
            probe = clause
            for name, pattern in _CHECK_COVERAGE.items():
                if name in checks:
                    probe = pattern.sub(" ", probe)
            if not _REQUIREMENT_MARKERS.search(probe):
                continue
            # 여기에 "이 절이 쓰는 어휘를 이미 측정하니 넘어간다"는 거부권이
            # 있었다. 근거로 적힌 예("부스트로 더 빨라지게", "카메라가 따라오게")는
            # _REQUIREMENT_MARKERS에 애초에 걸리지 않는다 — 순수 이동 문구는 마커가
            # 이미 거른다. 아무도 지나지 않는 문을 지키면서, 같은 낱말이 플레이어가
            # 아닌 대상을 가리키는 절을 묵살했다.
            #
            #   "적을 만들어 좌우로 자동 순찰하게 해줘"   ← `좌우`가 덮었다고 판단
            #   "이동과 점수 획득이 되는지 검증해줘"      ← `이동`이 덮었다고 판단
            #
            # 코퍼스 전체에서 묵살된 5개 절 중 3개가 이런 실제 공백이었고, 나머지
            # 2개는 위 `보드`/`board` 오탐이 우연히 상쇄된 것이었다. 양쪽을 함께
            # 고치면 거부권은 남길 이유가 없다.
            clause = clause[:120]
            if clause not in seen:
                seen.add(clause)
                found.append(clause)
    return found


# task_contract가 같은 문장을 다르게 읽어 두 번 사고가 났다 — 정책 게이트가 강제하는
# 키와 호스트가 측정하는 키가 갈라지면 올바른 구현이 떨어진다. 요청 문구 어휘는 이
# 모듈이 단일 출처이고, 아래 이름으로만 공유한다.
AD_SCHEME = _AD_SCHEME
ARROW_SCHEME = _ARROW_SCHEME
CAMERA_WORDS = _CAMERA_WORDS
CAMERA_FOLLOW_WORDS = _CAMERA_FOLLOW_WORDS

MUTATION_TOOLS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_remove_component",
    "unity_set_component_property", "unity_create_material", "unity_create_scene",
    "unity_open_scene", "unity_save_scene", "unity_refresh_assets", "unity_write_script",
    "unity_delete_script", "unity_install_level_loader", "unity_write_level",
    "unity_execute_menu_item",
}


def _has_word(lower: str, words: Iterable[str]) -> bool:
    """Match Korean by substring and Latin on latin-letter boundaries.

    Korean has no word delimiters, so substring matching is correct there.
    Latin needs a boundary or "remove" would match "move" and every deletion
    request would demand a movement measurement. `\\b` is wrong here because
    Hangul counts as a word character, so "Main Camera는" would not match
    "camera" — the boundary must be defined against latin characters only.
    """
    for word in words:
        if word.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lower):
                return True
        elif word in lower:
            return True
    return False


has_word = _has_word


def _decode(result: str) -> dict | None:
    try:
        value, _ = json.JSONDecoder().raw_decode(str(result).lstrip())
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, AttributeError):
        return None


def _ok(result: str) -> dict | None:
    value = _decode(result)
    if value and value.get("status") == "ok" and isinstance(value.get("result"), dict):
        return value["result"]
    return None


def _position(value: dict) -> tuple[float, float, float] | None:
    try:
        raw = value["transform"]["position"]
        if not isinstance(raw, list) or len(raw) != 3:
            return None
        return tuple(float(item) for item in raw)
    except (KeyError, TypeError, ValueError):
        return None


def _normalise_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


# A Unity compiler diagnostic always carries a CS code or says the build could
# not finish. Anything else read while stopped is a leftover from the Play
# session that just ended, and the console keeps it until the next compile.
_COMPILER_DIAGNOSTIC = re.compile(
    r"error CS\d+|\berror CS\b|compiler errors|Build completed with a result of 'Failed'",
    re.I,
)


def _compile_diagnostics(entries: list) -> list:
    """Separate real compiler errors from runtime errors left in the console.

    Classifying by "was Play Mode running when we asked" mislabels every
    runtime error that survives the stop, and a mislabelled entry becomes
    ``compile_not_ready``, which blocks *every* measurement and can roll a run
    back to a worse state. A live camera-follow run failed exactly that way:
    ``Tag: Ground is not defined`` — thrown from ``OnCollisionEnter`` — was
    counted as two compile errors after Play stopped.
    """
    return [
        entry
        for entry in entries
        if _COMPILER_DIAGNOSTIC.search(str((entry or {}).get("message", "")))
    ]


def _compact_entries(entries: list, limit: int = 20) -> list:
    """Keep receipts useful when one runtime error repeats every physics tick."""
    compact: list = []
    seen: set[str] = set()
    for entry in entries:
        key = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        compact.append(entry)
        if len(compact) >= limit:
            break
    return compact


@dataclass
class VerificationSpec:
    request: str
    enabled: bool
    asset_paths: list[str] = field(default_factory=list)
    scene_path: str | None = None
    # The request used runtime-behaviour language, whether or not a concrete
    # check could be derived from it.
    behaviour_requested: bool = False
    verification_requested: bool = False
    build_requested: bool = False
    require_gameplay: bool = False
    require_movement: bool = False
    require_jump: bool = False
    require_camera_follow: bool = False
    require_camera_fixed_z: bool = False
    require_camera_target: bool = False
    require_player_constraints: bool = False
    require_boost: bool = False
    require_bidirectional: bool = False
    require_level_marker: bool = False
    require_screenshot: bool = False
    require_idle_stability: bool = False
    require_jump_landing: bool = False
    require_left_boost: bool = False
    # 생성 요청이 씬에 남긴 결과를 재는 검사. 계층에 기본 오브젝트 말고 무엇이라도
    # 있어야 한다.
    require_scene_objects: bool = False
    # 입력 없이 스스로 움직여야 하는 대상이 실제로 움직이는지.
    require_autonomous_motion: bool = False
    autonomous_duration: float = 0.8
    autonomous_min_distance: float = 0.15
    # 렌더러 없는 Player가 verified로 나간 적이 있다(영수증 20260731_033338_195).
    # Rigidbody와 Collider만 요구하면 물리는 완벽히 동작하고 이동·점프·부스트·카메라가
    # 전부 실측 통과하는데 화면에는 아무것도 그려지지 않는다.
    require_visible_player: bool = False
    idle_duration: float = 0.5
    idle_max_delta_x: float = 0.05
    movement_duration: float = 1.0
    movement_min_distance: float = 2.0
    # Upper sanity bound. A repeated AddForce(..., Impulse) in FixedUpdate sends
    # the player 100+ units per second, which passed a min-distance-only check
    # while being obviously broken physics.
    movement_max_speed: float = 25.0
    # Which keys the request asked for. Verifying rightArrow against a script
    # that only reads A/D fails the game for the harness's own assumption.
    move_right_key: str = "rightArrow"
    move_left_key: str = "leftArrow"
    movement_keys_explicit: bool = False
    jump_key: str = "space"
    boost_duration: float = 0.5
    boost_min_ratio: float = 1.4
    # 하한만 있으면 "빨라지기는 했다"가 무엇이든 통과한다. 실측에서 0.5초에 140유닛을
    # 간 대시(일반 이동의 56배)가 그대로 통과했고, 플레이어가 화면 밖으로 나가 뒤이은
    # 카메라 측정까지 망가뜨렸다. 이동은 v1.11.4, 점프는 v1.11.9에서 같은 이유로 상한을
    # 받았다. 기록된 부스트 28건은 정상 1.0~2.3에 하나가 6.6, 고장난 것이 53~60이라
    # 10배면 양쪽에 여유가 있다.
    boost_max_ratio: float = 10.0
    jump_min_rise: float = 0.5
    # Upper sanity bound, the jump counterpart of movement_max_speed. A latch
    # that re-arms while the key is held applies AddForce(..., Impulse) on
    # several physics steps and stacks the launch velocity, which cleared a
    # min-rise-only check while sending the player ~20 units up and off the
    # platform. A single impulse at a typical jumpForce rises about 5 units.
    jump_max_rise: float = 10.0
    required_components: dict[str, list[str]] = field(default_factory=dict)

    def movement_max_distance(self, duration: float | None = None) -> float:
        return self.movement_max_speed * (
            self.movement_duration if duration is None else duration
        )

    @classmethod
    def from_request(cls, request: str, force: bool = False) -> "VerificationSpec":
        lower = request.lower()
        preflight = inspect_request(request, config.SCENE_PATH_POLICY)
        assets = preflight.asset_paths
        game = _has_word(lower, _GAME_WORDS)
        # Behaviour words stand alone; entity words ("플레이어") still need game
        # context so that "Player 이름을 바꿔줘" does not demand a Play Mode run.
        movement = _has_word(lower, _MOVEMENT_WORDS) or (
            game and _has_word(lower, _MOVEMENT_CONTEXT_WORDS)
        )
        jump = _has_word(lower, _JUMP_WORDS) or (
            game and _has_word(lower, _JUMP_CONTEXT_WORDS)
        )
        # A camera merely mentioned in a non-game request should not trigger an
        # input measurement. Games, or explicit follow/tracking wording, do.
        camera = _has_word(lower, _CAMERA_WORDS) and (
            game or _has_word(lower, _CAMERA_FOLLOW_WORDS)
        )
        boost = _has_word(lower, _BOOST_WORDS) or (
            movement and _has_word(lower, _BOOST_CONTEXT_WORDS)
        )
        # Boost is measured as a ratio against normal movement, so it implies it.
        movement = movement or boost
        level = _has_word(lower, _LEVEL_WORDS)
        script_classes = [os.path.splitext(os.path.basename(path))[0] for path in assets
                          if path.lower().endswith(".cs")]
        components: dict[str, list[str]] = {}
        if movement or jump:
            components["Player"] = ["Rigidbody", "Collider"]
            player_class = next((name for name in script_classes
                                 if "player" in name.lower() and "movement" in name.lower()), None)
            if player_class:
                components["Player"].append(player_class)
            elif "playermovement" in lower:
                # Preserve the older generic convention when no exact script
                # path was supplied in the request.
                components["Player"].append("PlayerMovement")
        if camera:
            components["Main Camera"] = ["Camera"]
            camera_class = next((name for name in script_classes if "camera" in name.lower()), None)
            if camera_class:
                components["Main Camera"].append(camera_class)
            elif "sidescrollercamera" in lower:
                components["Main Camera"].append("SideScrollerCamera")
        # Any extracted behaviour condition implies Play Mode: a check that is
        # never run must never count as passed.
        behaviour = movement or jump or camera or boost
        build = _has_word(lower, _BUILD_WORDS)
        # 생성 동사 하나만으로는 부족하다 — "스크립트를 만들어줘"는 씬에 아무것도
        # 남기지 않는 것이 정상이다. 씬에 실체로 존재해야 하는 대상을 함께 말했을
        # 때만 계층을 잰다.
        scene_objects = build and _has_word(lower, _CREATE_WORDS) and (
            _has_word(lower, _SCENE_OBJECT_WORDS) or game or level or behaviour
        )
        if _AD_SCHEME.search(request):
            right_key, left_key, keys_explicit = "d", "a", True
        elif _ARROW_SCHEME.search(request):
            right_key, left_key, keys_explicit = "rightArrow", "leftArrow", True
        else:
            right_key, left_key, keys_explicit = "rightArrow", "leftArrow", False
        jump_key = next(
            (key for pattern, key in _JUMP_KEY_SCHEMES if pattern.search(request)),
            "space",
        )
        return cls(
            request=preflight.normalized_request,
            enabled=force or build,
            asset_paths=assets,
            scene_path=preflight.canonical_scene_path,
            behaviour_requested=_has_word(lower, _BEHAVIOUR_HINT_WORDS),
            verification_requested=(
                _has_word(lower, _VERIFICATION_REQUEST_WORDS)
                or bool(_VERIFICATION_REQUEST_PHRASES.search(request))
            ),
            build_requested=build,
            require_autonomous_motion=_has_word(lower, _AUTONOMOUS_WORDS),
            require_scene_objects=scene_objects,
            # Player 컴포넌트를 요구하는 요청은 그 Player가 화면에 보여야 한다.
            require_visible_player="Player" in components,
            require_gameplay=game or level or behaviour,
            require_movement=movement,
            require_jump=jump,
            require_camera_follow=camera,
            require_camera_fixed_z=camera and (
                "z는 고정" in lower or "z 고정" in lower or "fixed z" in lower
            ),
            require_camera_target=camera and (
                "target이 null이 아니" in lower or "target is not null" in lower
            ),
            require_player_constraints=movement and (
                "z 이동과 회전을 고정" in lower
                or "z position and rotation" in lower
            ),
            require_boost=boost,
            require_bidirectional=movement and bool(
                _AD_SCHEME.search(request)
                or _has_word(lower, _BIDIRECTIONAL_WORDS)
                or (re.search(r"\ba\b", lower) and re.search(r"\bd\b", lower))
            ),
            require_level_marker=level,
            require_screenshot=game,
            require_idle_stability="무입력 0.5초" in lower or "idle 0.5" in lower,
            # Landing is measured as part of the jump arc, so it must not be
            # requested without a jump measurement to attach it to.
            require_jump_landing=jump and _has_word(lower, _LANDING_WORDS),
            move_right_key=right_key,
            move_left_key=left_key,
            movement_keys_explicit=keys_explicit,
            jump_key=jump_key,
            require_left_boost="a+leftshift" in lower.replace(" ", ""),
            required_components=components,
        )

    # Canonical check names shared by the receipt and the empty-spec guard.
    # (name, is-requested) — evaluated in a fixed order so receipts diff cleanly.
    def requested_checks(self) -> list[str]:
        flags = [
            ("scene_objects", self.require_scene_objects),
            ("player_visible", self.require_visible_player),
            ("gameplay", self.require_gameplay),
            ("autonomous_motion", self.require_autonomous_motion),
            ("movement", self.require_movement),
            ("bidirectional", self.require_bidirectional),
            ("idle_stability", self.require_idle_stability),
            ("jump", self.require_jump),
            ("jump_landing", self.require_jump_landing),
            ("camera_follow", self.require_camera_follow),
            ("camera_fixed_z", self.require_camera_fixed_z),
            ("camera_target", self.require_camera_target),
            ("player_constraints", self.require_player_constraints),
            ("boost", self.require_boost),
            ("left_boost", self.require_left_boost),
            ("level_marker", self.require_level_marker),
            ("screenshot", self.require_screenshot),
        ]
        names = [name for name, enabled in flags if enabled]
        names.extend(f"components:{target}" for target in sorted(self.required_components))
        return names

    def behaviour_checks(self) -> list[str]:
        """Requested checks that need an actual Play Mode measurement."""
        # scene_objects·player_visible는 Edit Mode 계층 조회로 끝난다. Play Mode
        # 증명을 요구하는 목록에 넣으면 빈 spec 가드의 뜻이 무너진다.
        static = {
            "components", "level_marker", "screenshot",
            "scene_objects", "player_visible",
        }
        return [
            name for name in self.requested_checks()
            if name.split(":")[0] not in static
        ]

    def unmapped_requirements(self) -> list[str]:
        """Request clauses no requested check will measure. Diagnostic only."""
        return _unmapped_requirements(self.request, self.requested_checks())

    def checklist(self) -> list[str]:
        checks = [
            "요청에 명시된 Assets 파일이 실제 디스크에 존재",
            "unity_get_state: 활성 씬이 저장됐고(isDirty=false) Play Mode가 아님",
            "unity_read_console types=error,exception: 컴파일 오류 0건",
        ]
        if self.require_scene_objects:
            checks.append(
                "unity_get_hierarchy: 활성 씬에 기본 오브젝트(Main Camera, "
                "Directional Light) 외의 오브젝트가 실제로 존재"
            )
        if self.require_visible_player:
            checks.append("Player 또는 그 자식에 Renderer가 있어 화면에 그려짐")
        if self.require_autonomous_motion:
            checks.append(
                f"입력을 주지 않은 {self.autonomous_duration}초 동안 Player가 아닌 "
                f"오브젝트 하나가 {self.autonomous_min_distance} 이상 스스로 이동"
            )
        for target, components in self.required_components.items():
            checks.append(f"{target} 컴포넌트 포함: {', '.join(components)}")
        if self.require_gameplay:
            checks.append("Play 진입 후 unity_wait, 런타임 오류 0건")
        if self.require_level_marker:
            checks.append("런타임 콘솔에 [LevelLoader] Loaded 마커")
        if self.require_movement:
            checks.append(
                f"{self.move_right_key} 입력 전후 Player X가 실제로 증가하되 "
                f"{self.movement_max_distance():.0f} 이내"
            )
        if self.require_idle_stability:
            checks.append(
                f"무입력 {self.idle_duration}초 Player X 변화가 "
                f"{self.idle_max_delta_x} 이하"
            )
        if self.require_jump:
            checks.append(
                f"{self.jump_key} 입력 전후 Player Y가 실제로 증가하되 "
                f"{self.jump_max_rise:.0f} 이내"
            )
        if self.require_camera_follow:
            checks.append("Player 이동과 같은 구간에 Main Camera X가 실제로 증가")
        if self.require_camera_fixed_z:
            checks.append("Player 이동 중 Main Camera Z 변화가 0.05 이하")
        if self.require_camera_target:
            checks.append("Play 진입 전 SideScrollerCamera.target이 null이 아님")
        if self.require_player_constraints:
            checks.append("Rigidbody가 Z 위치와 X/Y/Z 회전을 모두 고정")
        if self.require_boost:
            checks.append(
                f"D 이동 대비 D+LeftShift 이동 거리가 {self.boost_min_ratio}배 이상"
            )
        if self.require_left_boost:
            checks.append("A+LeftShift도 왼쪽으로 동일한 부스트 효과")
        if self.require_bidirectional:
            checks.append(
                f"D/A를 각각 {self.movement_duration}초 입력해 "
                f"{self.movement_min_distance} 이상 "
                f"{self.movement_max_distance():.0f} 이하 이동"
            )
        if self.require_jump_landing:
            checks.append("점프가 기준 높이 이상 상승한 뒤 시작 높이로 착지")
        if self.require_screenshot:
            checks.append("Play 중 Game 뷰 스크린샷 파일 생성")
        if self.require_gameplay:
            checks.append("입력 해제 후 Play 종료 및 unity_get_state isPlaying=false")
        return checks


@dataclass
class VerificationContract:
    spec: VerificationSpec
    project_dir: str
    session_scripts: set[str] = field(default_factory=set)
    state_seen: bool = False
    scene_path_seen: str | None = None
    scene_clean: bool = False
    final_stopped: bool = False
    compile_checked: bool = False
    compile_errors: list = field(default_factory=list)
    compile_error_count: int = 0
    played: bool = False
    playing: bool = False
    waited: bool = False
    runtime_checked: bool = False
    runtime_errors: list = field(default_factory=list)
    runtime_error_count: int = 0
    level_marker_seen: bool = False
    hierarchy_seen: bool = False
    scene_roots: list = field(default_factory=list)
    # Play Mode에서 입력 없이 잰 비-Player 오브젝트의 전/후 위치.
    autonomous_before: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    autonomous_after: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    observed_components: dict[str, list[str]] = field(default_factory=dict)
    observed_component_data: dict[str, dict[str, dict]] = field(default_factory=dict)
    latest_positions: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    latest_active: dict[str, bool] = field(default_factory=dict)
    # 검증이 실제로 플레이어를 데려간 모든 지점. 접촉 판단의 원자료다.
    player_samples: list = field(default_factory=list)
    movement_before: tuple[float, float, float] | None = None
    movement_after: tuple[float, float, float] | None = None
    camera_before: tuple[float, float, float] | None = None
    camera_after: tuple[float, float, float] | None = None
    jump_before: tuple[float, float, float] | None = None
    jump_after: tuple[float, float, float] | None = None
    jump_peak_y: float | None = None
    movement_input_seen: bool = False
    jump_input_seen: bool = False
    screenshot_path: str | None = None
    screenshot_in_play: bool = False
    input_released: bool = False
    tool_errors: list[str] = field(default_factory=list)
    play_active_confirmed: bool = False
    play_ended_unexpectedly: bool = False
    final_stop_requested: bool = False
    motion_before: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    motion_after: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    camera_motion_before: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    camera_motion_after: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    motion_duration: dict[str, float] = field(default_factory=dict)
    idle_before: tuple[float, float, float] | None = None
    idle_after: tuple[float, float, float] | None = None
    jump_landed: bool = False
    # 접촉 반응 기록용. Play Mode에서 입력 전/후로 잰 비-Player 오브젝트의 위치와
    # 활성 상태. 판정에는 쓰지 않는다(A0의 설계 입력).
    contact_before: dict[str, dict] = field(default_factory=dict)
    contact_after: dict[str, dict] = field(default_factory=dict)
    blocked_by: dict[str, list[str]] = field(default_factory=dict)
    policy_violations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 요청이 명시한 경로만 보면 정적 검사가 통째로 꺼진다. "새 씬을 만들고
        # ~를 구현해줘"류 요청은 스크립트 경로를 말하지 않으므로 `asset_paths`에
        # 씬 하나만 들어오고, **빌더가 스스로 만든 스크립트는 한 번도 검사되지
        # 않았다.** 2026-08-03 코인 실행이 `CompareTag("Coin")`을 담은 채
        # `verified`로 나간 것이 그 결과다 — 프로젝트 태그는 비어 있어서 트리거가
        # 걸리는 순간 `Tag: Coin is not defined`를 던진다. 이 규칙은 정확히 그
        # 경우를 위해 있었는데 대상 목록에 파일이 없어 돌지 않았다.
        #
        # 기록된 실행 109개의 빌더 산출 스크립트 158개를 재생하면 21개 실행에서
        # 위반 41건이 나오고, 현재 규칙이 모두 들어간 7/29 이후로 좁히면 78개 중
        # 5건이다. 다섯 건 모두 미정의 태그이고 오탐은 없었다.
        targets = list(dict.fromkeys(
            list(self.spec.asset_paths) + sorted(self.session_scripts)
        ))
        self.policy_violations = lint_scripts(
            self.spec.request, targets, self.project_dir
        )

    def block(self, stage: str, reason: str) -> None:
        self.blocked_by.setdefault(stage, [])
        if reason not in self.blocked_by[stage]:
            self.blocked_by[stage].append(reason)

    def prepare_call(self, name: str, args: dict) -> tuple[dict, str | None]:
        args = dict(args or {})
        if name in MUTATION_TOOLS:
            return args, f"Verification blocked mutation tool: {name}"
        if name == "unity_send_key" and not self.playing:
            return args, "Verification blocked input outside Play Mode"
        return args, None

    def begin_motion(self, name: str) -> bool:
        player = self.latest_positions.get("player")
        if player is None:
            return False
        self.motion_before[name] = player
        camera = self.latest_positions.get("main camera")
        if camera is not None:
            self.camera_motion_before[name] = camera
        return True

    def rightward_delta(self) -> float | None:
        """X change of the canonical rightward sample, or None if unmeasured."""
        before, after = self.motion_before.get("rightArrow"), self.motion_after.get("rightArrow")
        return None if before is None or after is None else after[0] - before[0]

    def end_motion(self, name: str) -> None:
        player = self.latest_positions.get("player")
        if player is not None:
            self.motion_after[name] = player
        camera = self.latest_positions.get("main camera")
        if camera is not None:
            self.camera_motion_after[name] = camera

    def observe(self, name: str, args: dict, result: str) -> None:
        data = _ok(result)
        if data is None:
            return
        if name == "unity_get_state":
            self.state_seen = True
            self.playing = bool(data.get("isPlaying"))
            scene = data.get("activeScene") or {}
            self.scene_path_seen = _normalise_path(scene.get("path", "")) or None
            self.scene_clean = bool(self.scene_path_seen) and not bool(scene.get("isDirty", True))
            if self.played and self.playing:
                self.play_active_confirmed = True
            # isPlaying can temporarily read false while Unity reloads the
            # domain after accepting a play request. It is an unexpected end
            # only after this contract has observed Play Mode active once.
            if self.play_active_confirmed and not self.playing:
                self.final_stopped = True
                if not self.final_stop_requested:
                    self.play_ended_unexpectedly = True
        elif name == "unity_play_mode":
            action = str(args.get("action", "")).lower()
            if action == "play":
                self.played = True
                self.playing = True
                self.play_active_confirmed = False
                self.waited = False
                self.runtime_checked = False
                self.final_stopped = False
                self.final_stop_requested = False
            elif action == "stop":
                self.playing = False
                self.final_stop_requested = True
        elif name == "unity_wait" and self.playing:
            self.waited = True
        elif name == "unity_read_console":
            entries = data.get("entries") if isinstance(data.get("entries"), list) else []
            if "[LevelLoader] Loaded" in str(result):
                self.level_marker_seen = True
            requested = str(args.get("types", "")).lower()
            is_error_check = not requested or "error" in requested or "exception" in requested
            if is_error_check:
                if self.playing and self.waited:
                    self.runtime_checked = True
                    self.runtime_errors = _compact_entries(self.runtime_errors + entries)
                    self.runtime_error_count = len(self.runtime_errors)
                elif not self.playing:
                    self.compile_checked = True
                    diagnostics = _compile_diagnostics(entries)
                    self.compile_error_count = len(diagnostics)
                    self.compile_errors = _compact_entries(diagnostics)
                    # Errors thrown by the Play session that just ended are still
                    # real failures; they belong to the runtime tally that already
                    # measured them, not to the compile gate that blocks every
                    # other check.
                    leftovers = [e for e in entries if e not in diagnostics]
                    if leftovers and self.played:
                        self.runtime_errors = _compact_entries(
                            self.runtime_errors + leftovers
                        )
                        self.runtime_error_count = len(self.runtime_errors)
        elif name == "unity_get_hierarchy":
            scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
            wanted = self.spec.scene_path or self.scene_path_seen
            chosen = next(
                (
                    scene for scene in scenes
                    if isinstance(scene, dict)
                    and wanted
                    and _normalise_path(str(scene.get("path", ""))) == wanted
                ),
                None,
            )
            if chosen is None:
                chosen = next((s for s in scenes if isinstance(s, dict)), None)
            if chosen is not None:
                roots = chosen.get("rootObjects")
                self.scene_roots = roots if isinstance(roots, list) else []
                self.hierarchy_seen = True
        elif name == "unity_get_gameobject":
            target = str(args.get("target", "")).strip()
            pos = _position(data)
            # 접촉 반응은 오브젝트를 Destroy 하거나 SetActive(false) 한다. 위치만
            # 보면 후자를 놓치므로 활성 상태를 함께 남긴다.
            if "activeSelf" in data:
                self.latest_active[target.lower()] = bool(data.get("activeSelf"))
            if pos is not None:
                self.latest_positions[target.lower()] = pos
                if target.lower() == "player" and pos not in self.player_samples:
                    self.player_samples.append(pos)
                if target.lower() == "player":
                    if self.movement_input_seen:
                        self.movement_after = pos
                    if self.jump_input_seen:
                        if self.jump_after is None or pos[1] > self.jump_after[1]:
                            self.jump_after = pos
                        self.jump_peak_y = max(self.jump_peak_y or pos[1], pos[1])
                elif target.lower() == "main camera" and self.movement_input_seen:
                    self.camera_after = pos
            components = data.get("components") or []
            observed = [
                str(item.get("type", "")) for item in components if isinstance(item, dict)
            ]
            if observed:
                self.observed_components[target] = observed
                self.observed_component_data[target] = {
                    str(item.get("type", "")): item.get("data")
                    for item in components
                    if isinstance(item, dict) and isinstance(item.get("data"), dict)
                }
        elif name == "unity_send_key":
            key = str(args.get("key", "")).lower()
            action = str(args.get("action", "tap")).lower()
            if key in {"rightarrow", "right", "d"} and action in {"press", "tap"}:
                self.movement_input_seen = True
                self.movement_before = self.latest_positions.get("player")
                self.camera_before = self.latest_positions.get("main camera")
            if key in {"space", "spacebar", "w", "uparrow"} and action in {"press", "tap"}:
                self.jump_input_seen = True
                self.jump_before = self.latest_positions.get("player")
            if action == "release":
                self.input_released = True
        elif name == "unity_get_input_state":
            self.input_released = not data.get("held") and not data.get("pendingReleases")
        elif name == "unity_screenshot":
            path = str(data.get("path", ""))
            if path and not os.path.isabs(path):
                path = os.path.join(self.project_dir, path)
            self.screenshot_path = os.path.abspath(path) if path else None
            self.screenshot_in_play = self.playing

    @staticmethod
    def _has_component(observed: Iterable[str], required: str) -> bool:
        required = required.lower()
        if required == "collider":
            return any(item.lower().split(".")[-1].endswith("collider") for item in observed)
        return any(item.lower() == required or item.lower().endswith("." + required)
                   for item in observed)

    def created_objects(self) -> list[str]:
        """Active root objects a new scene does not come with on its own."""
        return [
            str(node.get("name", ""))
            for node in self.scene_roots
            if isinstance(node, dict)
            and node.get("active", True)
            and str(node.get("name", "")).strip().lower() not in _DEFAULT_SCENE_OBJECTS
        ]

    def autonomous_candidates(self, limit: int = 6) -> list[str]:
        """Objects that could be the thing moving on its own.

        Named from the observed hierarchy rather than a naming convention: the
        request says "적", the model may call it Enemy, Patroller or Monster.
        Player is excluded because the host drives it with input; the ground and
        the platform stay in because a moving platform is the same check.
        """
        return [
            name for name in self.created_objects()
            if name.strip().lower() != "player"
        ][:limit]

    def autonomous_motion_delta(self) -> tuple[str, float] | None:
        """Largest distance any sampled object covered with no input given."""
        best: tuple[str, float] | None = None
        for name, before in self.autonomous_before.items():
            after = self.autonomous_after.get(name)
            if after is None:
                continue
            moved = sum((a - b) ** 2 for a, b in zip(after, before)) ** 0.5
            if best is None or moved > best[1]:
                best = (name, moved)
        return best

    def player_paths(self) -> list[tuple]:
        """플레이어가 실제로 통과한 구간들. 점이 아니라 선분이다.

        표본은 측정 구간의 양 끝에서만 찍힌다. D 측정의 `(0,1,0) → (4.95,1,0)`은
        남지만 그 사이를 지나간 `(2,1,0)`은 남지 않아, 코인 바로 아래를 지나가고도
        거리가 2.05로 기록됐다(실제 수직 거리 1.0).

        **아무 표본끼리 이으면 안 된다.** 측정 구간 사이에는 `restart_play()`로
        플레이어가 스폰으로 되돌아가므로, 그 둘을 선분으로 이으면 실제로 지나가지
        않은 경로를 지어내게 된다. 그래서 "한 번의 연속 이동"이 보장된 쌍만 쓴다.
        """
        paths = []
        for label, before in self.motion_before.items():
            after = self.motion_after.get(label)
            if after is not None:
                paths.append((before, after))
        # 점프는 같은 X에서 수직으로 오르내린 구간이다.
        if self.jump_before is not None and self.jump_peak_y is not None:
            paths.append((
                self.jump_before,
                (self.jump_before[0], self.jump_peak_y, self.jump_before[2]),
            ))
        return paths

    @staticmethod
    def _distance_to_segment(pos, start, end) -> float:
        span = tuple(e - s for e, s in zip(end, start))
        length2 = sum(v * v for v in span)
        if length2 <= 1e-12:
            return sum((p - s) ** 2 for p, s in zip(pos, start)) ** 0.5
        t = sum((p - s) * v for p, s, v in zip(pos, start, span)) / length2
        t = max(0.0, min(1.0, t))
        closest = tuple(s + t * v for s, v in zip(start, span))
        return sum((p - c) ** 2 for p, c in zip(pos, closest)) ** 0.5

    def nearest_player_distance(self, pos) -> float | None:
        """이 좌표에 플레이어가 가장 가까이 왔던 거리."""
        if pos is None:
            return None
        candidates = [
            self._distance_to_segment(pos, start, end)
            for start, end in self.player_paths()
        ]
        candidates += [
            sum((a - b) ** 2 for a, b in zip(pos, sample)) ** 0.5
            for sample in self.player_samples
        ]
        return min(candidates) if candidates else None

    def contact_report(self) -> list[dict]:
        """접촉 반응의 재료. 판정하지 않고 거리만 남긴다.

        A0의 막힌 지점은 "호스트가 접촉을 일으킬 수 없다"가 아니라 **"안 사라졌을 때
        고장인지 안 닿은 건지 모른다"**는 비대칭이었다. 플레이어가 실제로 간 지점들을
        갖고 있으면 그 비대칭은 거리로 표현된다 — 가까이 갔는데 그대로면 고장이고,
        멀리 있었으면 판정할 수 없다.

        **불린으로 남기지 않는다.** 첫 실측(2026-08-05)이 그 이유를 보여줬다. X 구간과
        대표 Y로 경계 상자를 만들어 `player_passed`를 계산했더니, 점프로 닿은
        `Coin_2`(y 4)를 "안 닿았다"로 판정했다. 점프 높이를 상자에 넣으면 이번에는
        `Coin_3`(−2, 3)처럼 **플레이어가 그 x에 있을 때는 땅에 있었고 그 높이에 있을
        때는 다른 x에 있었던** 오브젝트까지 "지나갔다"가 된다. 상자는 경로가 아니다.

        그래서 임계값을 굽지 않고 **가장 가까웠던 거리**를 그대로 남긴다. 표본이
        쌓이면 그때 판정 기준을 고른다.
        """
        report: list[dict] = []
        for name, before in self.contact_before.items():
            after = self.contact_after.get(name)
            pos = before.get("position")
            gone = None
            if after is not None:
                gone = bool(after.get("missing")) or (
                    before.get("active") and not after.get("active")
                )
            distance = self.nearest_player_distance(pos)
            report.append({
                "object": name,
                "position": [round(v, 3) for v in pos] if pos else None,
                "nearest_player_distance": (
                    None if distance is None else round(distance, 3)
                ),
                "disappeared": gone,
                # 후보 대부분은 상호작용 대상이 아니라 구조물이다(Ground, Floor,
                # Platform, FlagPole). 첫 15건 표본에서 그 둘이 섞여 거리 분포를
                # 읽을 수 없었다. 붙은 스크립트가 그 둘을 가른다 — 코인·아이템은
                # 마커나 동작 스크립트를 갖고, 바닥은 갖지 않는다.
                "scripts": self._object_scripts(name),
            })
        return report

    # Unity가 기본으로 붙이는 것들. 이것만 있으면 구조물이다.
    _BUILTIN_COMPONENTS = frozenset({
        "transform", "meshfilter", "meshrenderer", "boxcollider", "spherecollider",
        "capsulecollider", "meshcollider", "rigidbody", "camera", "audiolistener",
        "light", "universaladditionallightdata", "recttransform",
    })

    def _object_scripts(self, name: str) -> list[str]:
        """이 오브젝트에 붙은 사용자 스크립트 이름."""
        node = self._find_node(name)
        components = (node or {}).get("components")
        if not isinstance(components, list):
            return []
        return [
            str(item) for item in components
            if str(item).lower().split(".")[-1] not in self._BUILTIN_COMPONENTS
        ]

    def _find_node(self, name: str) -> dict | None:
        """Locate one object anywhere in the observed hierarchy."""
        wanted = name.strip().lower()
        stack = [node for node in self.scene_roots if isinstance(node, dict)]
        while stack:
            node = stack.pop()
            if str(node.get("name", "")).strip().lower() == wanted:
                return node
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(item for item in children if isinstance(item, dict))
        return None

    @classmethod
    def _subtree_has_renderer(cls, node: dict) -> bool:
        """Whether this object or any descendant draws something.

        The mesh may legitimately sit on a child — a Player made of a body and a
        head is still visible. Requiring the renderer on the object itself would
        fail that structure for no reason.
        """
        components = node.get("components")
        if isinstance(components, list) and any(
            str(item).lower().split(".")[-1].endswith("renderer")
            for item in components
        ):
            return True
        children = node.get("children")
        return isinstance(children, list) and any(
            cls._subtree_has_renderer(item)
            for item in children if isinstance(item, dict)
        )

    def _player_renderer_seen(self) -> bool | None:
        """True/False when a Player was observed, None when it never was."""
        node = self._find_node("Player")
        if node is not None:
            return self._subtree_has_renderer(node)
        observed = self.observed_components.get("Player")
        if observed:
            # unity_get_gameobject lists only the object's own components, so a
            # child mesh is invisible here. Used only when no hierarchy read
            # happened at all.
            return any(
                item.lower().split(".")[-1].endswith("renderer") for item in observed
            )
        return None

    def _blocked_movement_direction(self, d, a) -> str | None:
        """Which direction a wall stopped, or None if this is not that shape.

        v1.11.15가 실측한 실패 모드다 — 시작 지점 옆에 플랫폼이 있으면 그 방향의
        이동 거리가 짧아지고, 호스트는 그것을 입력 코드의 결함으로 보고한다.
        repair는 멀쩡한 스크립트를 고치러 가고, **코드를 고쳐서는 절대 통과할 수
        없다.**

        기록된 d/a 동시 측정 119건의 비율 분포가 이 판정을 가능하게 한다.

            정상          0.96 ~ 1.00   (대다수)
            (빈 구간)     0.43 ~ 0.95
            막힘          0.00 ~ 0.42

        0.5는 그 빈 구간 한가운데다. 3.53/5.05(0.70)처럼 애매한 표본은 건드리지
        않는다.

        두 가지를 반드시 배제한다.
        - **양쪽 다 실패**하면 장애물이 아니라 입력 코드다. 한쪽이 최소 거리를
          넘겨 "이 방향은 된다"를 증명했을 때만 성립한다.
        - **폭주 물리**(0.5초에 115유닛)는 비율이 낮게 나오지만 원인이 다르다.
          성공한 쪽이 상한 안에 있어야 한다.
        """
        if None in d or None in a:
            return None
        forward = d[1][0] - d[0][0]
        backward = -(a[1][0] - a[0][0])
        pairs = (("d", forward, self.motion_duration.get("d")),
                 ("a", backward, self.motion_duration.get("a")))
        (short_name, short, _), (long_name, long_, long_duration) = sorted(
            pairs, key=lambda item: item[1]
        )
        if long_ < self.spec.movement_min_distance:
            return None                       # 양쪽 다 못 갔다 — 입력 코드다
        if long_ > self.spec.movement_max_distance(long_duration):
            return None                       # 폭주 물리는 별개 결함이다
        if short >= long_ * _BLOCKED_PATH_RATIO:
            return None
        return short_name

    def _camera_player_gap(self, label: str | None) -> float | None:
        """Distance between the camera and the player when a motion started.

        The follow check compares displacements, and a camera sitting on the
        player displaces identically — a first-person view passes it perfectly.
        Separation is the property that distinguishes observing from being the
        viewpoint, and it is computable from positions already collected.
        """
        camera = (
            self.camera_motion_before.get(label) if label else self.camera_before
        )
        player = self.motion_before.get(label) if label else self.movement_before
        if camera is None or player is None:
            return None
        return sum((c - p) ** 2 for c, p in zip(camera, player)) ** 0.5

    def failures(self) -> list[str]:
        failed: list[str] = [f"tool_error:{item}" for item in self.tool_errors]
        # A request that talks about runtime behaviour but yields no measurable
        # condition must never be reported as verified. Previously the empty
        # failure list read as success without Play Mode ever running.
        if (
            self.spec.enabled
            and self.spec.behaviour_requested
            and not self.spec.behaviour_checks()
        ):
            failed.append("verification_spec_empty")
        # The same refusal for a request that asks for proof in words this module
        # cannot map to any check at all. Without this, "실제로 생성되는지
        # 검증까지 끝내줘" produced `verified` with an empty measured set: the
        # host claimed success for something it never looked at.
        elif (
            self.spec.enabled
            and self.spec.build_requested
            and self.spec.verification_requested
            # scene_objects는 이 거절을 혼자 취소하지 못한다. 그 검사는 Edit Mode
            # 계층만 보므로 "10x20 격자를 Awake에서 코드로 생성"처럼 런타임에
            # 만들어지는 것에 대해서는 아무 말도 하지 않는다. 호스트 오브젝트 하나가
            # 씬에 있다는 이유로 v1.11.13이 세운 거절이 풀리면, 이 가드가 막으려던
            # 바로 그 영수증이 다시 나온다.
            and not [
                name for name in self.spec.requested_checks()
                if name != "scene_objects"
            ]
        ):
            failed.append("verification_spec_empty")
        failed.extend(f"policy_lint:{item}" for item in self.policy_violations)
        failed.extend(
            f"blocked:{stage}:{reason}"
            for stage, reasons in sorted(self.blocked_by.items())
            for reason in reasons
        )
        for path in self.spec.asset_paths:
            if not os.path.exists(os.path.join(self.project_dir, path)):
                failed.append(f"asset_missing:{path}")
        if not self.state_seen:
            failed.append("state_not_observed")
        if self.spec.scene_path and self.scene_path_seen != self.spec.scene_path:
            failed.append(f"wrong_active_scene:{self.scene_path_seen or 'unknown'}")
        if self.state_seen and not self.scene_clean:
            failed.append("scene_not_saved")
        if not self.compile_checked:
            failed.append("compile_not_checked")
        elif self.compile_error_count:
            failed.append(f"compile_errors:{self.compile_error_count}")
        if self.spec.require_scene_objects:
            if not self.hierarchy_seen:
                failed.append("scene_contents_not_observed")
            elif not self.created_objects():
                failed.append("scene_has_no_created_objects")
        if self.spec.require_visible_player:
            renderer = self._player_renderer_seen()
            if renderer is None:
                failed.append("player_visibility_not_measured")
            elif not renderer:
                failed.append("player_has_no_renderer")
        for target, required in self.spec.required_components.items():
            observed = self.observed_components.get(target, [])
            for component in required:
                if not self._has_component(observed, component):
                    failed.append(f"component_missing:{target}:{component}")
        if self.spec.require_player_constraints:
            player_data = self.observed_component_data.get("Player", {})
            rigidbody = next(
                (data for name, data in player_data.items()
                 if name.lower().endswith(".rigidbody") or name.lower() == "rigidbody"),
                None,
            )
            constraints = rigidbody.get("constraints") if rigidbody else None
            if not isinstance(constraints, int):
                failed.append("rigidbody_constraints_not_observed")
            elif (constraints & 8) != 8 or (constraints & 112) != 112:
                failed.append(f"rigidbody_constraints_incomplete:{constraints}")
        if self.spec.require_camera_target:
            camera_data = self.observed_component_data.get("Main Camera", {})
            side_camera = next(
                (data for name, data in camera_data.items()
                 if name.lower().endswith("sidescrollercamera")),
                None,
            )
            target = side_camera.get("target") if side_camera else None
            if not isinstance(target, dict) or not target.get("instanceID"):
                failed.append("camera_target_null")
        if self.spec.require_gameplay:
            if "gameplay" in self.blocked_by:
                pass
            elif not self.played:
                failed.append("play_mode_not_tested")
            elif not self.play_active_confirmed:
                failed.append("play_mode_not_active_after_start")
            if "gameplay" not in self.blocked_by and self.play_ended_unexpectedly:
                failed.append("play_mode_ended_unexpectedly")
            if "gameplay" in self.blocked_by:
                pass
            elif not self.waited:
                failed.append("runtime_wait_missing")
            if "gameplay" in self.blocked_by:
                pass
            elif not self.runtime_checked:
                failed.append("runtime_console_not_checked")
            elif self.runtime_error_count:
                failed.append(f"runtime_errors:{self.runtime_error_count}")
        if self.spec.require_level_marker and not self.level_marker_seen:
            failed.append("level_loaded_marker_missing")
        if self.spec.require_autonomous_motion:
            best = self.autonomous_motion_delta()
            if "gameplay" in self.blocked_by or "movement" in self.blocked_by:
                pass
            elif best is None:
                failed.append("autonomous_motion_not_measured")
            elif best[1] < self.spec.autonomous_min_distance:
                failed.append("no_object_moved_on_its_own")
        if self.spec.require_movement:
            # "rightArrow" is the canonical label for the rightward sample; the
            # bidirectional "d" sample proves the same capability when the
            # request asked for A/D, so accept either.
            before = after = None
            for label in ("rightArrow", "d"):
                if (
                    self.motion_before.get(label) is not None
                    and self.motion_after.get(label) is not None
                ):
                    before, after = self.motion_before[label], self.motion_after[label]
                    break
            if before is None:
                before, after = self.movement_before, self.movement_after
            if "movement" in self.blocked_by:
                pass
            elif before is None or after is None:
                failed.append("player_movement_not_measured")
            elif after[0] - before[0] <= 1e-3:
                failed.append("player_did_not_move_right")
            elif after[0] - before[0] > self.spec.movement_max_distance():
                failed.append("player_moved_too_far")
        if self.spec.require_idle_stability:
            if "movement" in self.blocked_by:
                pass
            elif self.idle_before is None or self.idle_after is None:
                failed.append("idle_stability_not_measured")
            elif abs(self.idle_after[0] - self.idle_before[0]) > self.spec.idle_max_delta_x:
                failed.append("idle_drift_too_large")
        if self.spec.require_bidirectional:
            d = self.motion_before.get("d"), self.motion_after.get("d")
            a = self.motion_before.get("a"), self.motion_after.get("a")
            blocked_direction = self._blocked_movement_direction(d, a)
            if "movement" in self.blocked_by:
                pass
            elif blocked_direction is not None:
                # 한쪽은 제대로 갔는데 반대쪽만 짧다. 입력 코드가 원인이라면 양쪽이
                # 같이 실패한다 — 이 형태는 그 방향에 장애물이 있다는 뜻이다.
                failed.append(f"movement_path_blocked:{blocked_direction}")
            else:
                if None in d:
                    failed.append("d_movement_not_measured")
                elif d[1][0] - d[0][0] < self.spec.movement_min_distance:
                    failed.append("d_did_not_move_right")
                elif d[1][0] - d[0][0] > self.spec.movement_max_distance(
                    self.motion_duration.get("d")
                ):
                    failed.append("d_moved_too_far")
                if None in a:
                    failed.append("a_movement_not_measured")
                elif a[1][0] - a[0][0] > -self.spec.movement_min_distance:
                    failed.append("a_did_not_move_left")
                elif a[0][0] - a[1][0] > self.spec.movement_max_distance(
                    self.motion_duration.get("a")
                ):
                    failed.append("a_moved_too_far")
        if self.spec.require_jump:
            if "jump" in self.blocked_by:
                pass
            elif self.jump_before is None or self.jump_peak_y is None:
                failed.append("player_jump_not_measured")
            elif self.jump_peak_y - self.jump_before[1] < self.spec.jump_min_rise:
                failed.append("player_did_not_jump")
            elif self.jump_peak_y - self.jump_before[1] > self.spec.jump_max_rise:
                failed.append("player_jumped_too_high")
            if (
                self.spec.require_jump_landing
                and "jump" not in self.blocked_by
                and not self.jump_landed
            ):
                failed.append("player_did_not_land")
        if self.spec.require_camera_follow:
            label_used = next((
                label for label in ("d", "rightArrow")
                if self.camera_motion_before.get(label) is not None
            ), None)
            camera_pair = (
                (self.camera_motion_before[label_used], self.camera_motion_after.get(label_used))
                if label_used else (self.camera_before, self.camera_after)
            )
            if "camera" in self.blocked_by:
                pass
            elif None in camera_pair:
                failed.append("camera_follow_not_measured")
            # 여기에는 상한이 없다. 이동은 v1.11.4, 점프는 v1.11.9에서 하한만으로는
            # 부족하다는 이유로 상한을 받았는데 카메라만 남아 있다. 다만 오늘 기록된
            # 카메라 측정 20건을 재생해 보면 최종 측정의 카메라 이동이 전부 플레이어
            # 이동과 0.04 이내로 일치했다 — 달아나는 카메라의 실측 사례가 아직 없다.
            # 사례 없이 검사를 늘리는 것은 v1.11.7의 `.bounds`가 v1.11.10에서
            # 되돌려진 방식이므로, 상한은 실제로 관측될 때 넣는다.
            elif camera_pair[1][0] - camera_pair[0][0] <= 1e-3:
                failed.append("camera_did_not_follow")
            else:
                # 플레이어에 붙은 1인칭 시점 카메라는 변위가 정확히 같아서 위 검사를
                # **완벽하게** 통과한다. 사용자가 직접 돌려보고 "카메라가 관찰이 아니라
                # 시점으로 되어 있다"고 보고한 형태이고, 영수증에는 델타만 남아 있어
                # 사후에 구분할 수도 없었다. 관찰하려면 떨어져 있어야 한다.
                gap = self._camera_player_gap(label_used)
                if gap is not None and gap < _CAMERA_MIN_GAP:
                    failed.append("camera_is_player_viewpoint")
        if self.spec.require_camera_fixed_z:
            camera_pairs = [
                (before, self.camera_motion_after.get(label))
                for label, before in self.camera_motion_before.items()
                if self.camera_motion_after.get(label) is not None
            ]
            if "camera" in self.blocked_by:
                pass
            elif not camera_pairs:
                failed.append("camera_fixed_z_not_measured")
            elif any(abs(after[2] - before[2]) > 0.05 for before, after in camera_pairs):
                failed.append("camera_z_changed")
        if self.spec.require_boost:
            normal_before = self.motion_before.get("boost_normal")
            normal_after = self.motion_after.get("boost_normal")
            boost_before = self.motion_before.get("boost_shift")
            boost_after = self.motion_after.get("boost_shift")
            if "boost" in self.blocked_by:
                pass
            elif None in (normal_before, normal_after, boost_before, boost_after):
                failed.append("boost_not_measured")
            else:
                normal = abs(normal_after[0] - normal_before[0])
                boosted = abs(boost_after[0] - boost_before[0])
                if normal <= 1e-3 or boosted < normal * self.spec.boost_min_ratio:
                    failed.append("boost_distance_too_short")
                elif boosted > normal * self.spec.boost_max_ratio:
                    failed.append("boost_moved_too_far")
            if self.spec.require_left_boost:
                left_before = self.motion_before.get("boost_left")
                left_after = self.motion_after.get("boost_left")
                if "boost" in self.blocked_by:
                    pass
                elif None in (left_before, left_after):
                    failed.append("left_boost_not_measured")
                else:
                    left = abs(left_after[0] - left_before[0])
                    if left_after[0] >= left_before[0] or normal <= 1e-3 or (
                        left < normal * self.spec.boost_min_ratio
                    ):
                        failed.append("left_boost_distance_too_short")
        if self.spec.require_screenshot:
            if "screenshot" in self.blocked_by:
                pass
            elif not self.screenshot_in_play or not self.screenshot_path:
                failed.append("play_screenshot_missing")
            elif not os.path.exists(self.screenshot_path):
                failed.append("screenshot_file_missing")
        if self.spec.require_gameplay:
            if (
                "gameplay" not in self.blocked_by
                and not self.input_released
                and (self.spec.require_movement or self.spec.require_jump)
            ):
                failed.append("simulated_input_not_released")
            if "gameplay" not in self.blocked_by and not self.final_stopped:
                failed.append("play_mode_not_stopped")
        return failed

    def missing_verification(self) -> list[str]:
        return self.failures()

    def measured_checks(self) -> list[str]:
        """Checks for which a real measurement exists, regardless of pass/fail."""
        def pair(before: dict, after: dict, *labels: str) -> bool:
            return any(
                before.get(label) is not None and after.get(label) is not None
                for label in labels
            )

        measured = {
            "scene_objects": self.hierarchy_seen,
            "player_visible": self._player_renderer_seen() is not None,
            "autonomous_motion": self.autonomous_motion_delta() is not None,
            "gameplay": self.played and self.waited and self.runtime_checked,
            "movement": pair(self.motion_before, self.motion_after, "rightArrow")
                        or (self.movement_before is not None
                            and self.movement_after is not None),
            "bidirectional": pair(self.motion_before, self.motion_after, "d")
                             and pair(self.motion_before, self.motion_after, "a"),
            "idle_stability": self.idle_before is not None and self.idle_after is not None,
            "jump": self.jump_before is not None and self.jump_peak_y is not None,
            "camera_follow": pair(self.camera_motion_before, self.camera_motion_after,
                                  "d", "rightArrow")
                             or (self.camera_before is not None
                                 and self.camera_after is not None),
            "camera_fixed_z": any(
                self.camera_motion_after.get(label) is not None
                for label in self.camera_motion_before
            ),
            "camera_target": bool(self.observed_component_data.get("Main Camera")),
            "player_constraints": bool(self.observed_component_data.get("Player")),
            "boost": pair(self.motion_before, self.motion_after,
                          "boost_normal") and pair(self.motion_before,
                                                   self.motion_after, "boost_shift"),
            "left_boost": pair(self.motion_before, self.motion_after, "boost_left"),
            "level_marker": self.level_marker_seen,
            "screenshot": bool(self.screenshot_path) and self.screenshot_in_play,
        }
        # jump_landing rides along with the jump arc measurement.
        measured["jump_landing"] = measured["jump"]
        names = []
        for name in self.spec.requested_checks():
            if name.startswith("components:"):
                if self.observed_components.get(name.split(":", 1)[1]):
                    names.append(name)
            elif measured.get(name):
                names.append(name)
        return names

    def check_report(self) -> dict:
        """Requested vs actually measured checks, for the verification receipt."""
        requested = self.spec.requested_checks()
        measured = self.measured_checks()
        measured_set = set(measured)
        return {
            "requested_checks": requested,
            "measured_checks": measured,
            "skipped_checks": [name for name in requested if name not in measured_set],
        }

    def evidence(self) -> dict:
        def delta(before, after):
            return None if before is None or after is None else [
                round(after[i] - before[i], 6) for i in range(3)
            ]
        return {
            "active_scene": self.scene_path_seen,
            "scene_clean": self.scene_clean,
            "compile": {
                "checked": self.compile_checked, "error_count": self.compile_error_count,
                "unique_errors": self.compile_errors,
            },
            "runtime": {
                "played": self.played, "waited": self.waited,
                "checked": self.runtime_checked, "error_count": self.runtime_error_count,
                "unique_errors": self.runtime_errors,
                "level_loaded_marker": self.level_marker_seen,
            },
            "scene_objects": self.created_objects() if self.hierarchy_seen else None,
            "player_has_renderer": self._player_renderer_seen(),
            "autonomous_motion": (
                None if (best := self.autonomous_motion_delta()) is None
                else {"object": best[0], "distance": round(best[1], 4)}
            ),
            # 판정에 쓰지 않는 기록. A0(접촉 반응 검사)의 설계 입력이다.
            "contact_candidates": self.contact_report() or None,
            "components": self.observed_components,
            "component_data": self.observed_component_data,
            "player_movement_delta": delta(self.movement_before, self.movement_after),
            "player_jump_delta": delta(self.jump_before, self.jump_after),
            "player_jump_peak_y": self.jump_peak_y,
            "camera_follow_delta": delta(self.camera_before, self.camera_after),
            # 델타만 남기면 플레이어에 붙은 시점 카메라와 제대로 따라오는 카메라를
            # 영수증에서 구분할 수 없다. 둘 다 같은 변위를 낸다.
            "camera_player_gap": next(
                (
                    round(gap, 3)
                    for gap in (
                        self._camera_player_gap(label)
                        for label in ("d", "rightArrow", None)
                    )
                    if gap is not None
                ),
                None,
            ),
            "screenshot": self.screenshot_path,
            "screenshot_captured_in_play": self.screenshot_in_play,
            "input_released": self.input_released,
            "final_play_mode_stopped": self.final_stopped,
            "play_active_confirmed": self.play_active_confirmed,
            "play_ended_unexpectedly": self.play_ended_unexpectedly,
            "motion_deltas": {
                name: delta(before, self.motion_after.get(name))
                for name, before in self.motion_before.items()
            },
            "camera_motion_deltas": {
                name: delta(before, self.camera_motion_after.get(name))
                for name, before in self.camera_motion_before.items()
            },
            "motion_durations": self.motion_duration,
            "idle_delta": delta(self.idle_before, self.idle_after),
            "jump_landed": self.jump_landed,
            "blocked_by": self.blocked_by,
            "policy_lint": self.policy_violations,
            "tool_errors": self.tool_errors,
        }


# Which canonical check each failure code belongs to. Used to tell a genuine
# regression ("a check that passed now fails") apart from a first measurement
# ("a check that was blocked before is finally being measured, and it fails").
# Order matters: longer prefixes must precede the shorter ones they extend.
_FAILURE_CHECK_PREFIXES = (
    ("scene_has_no_created_objects", "scene_objects"),
    ("scene_contents_not_observed", "scene_objects"),
    ("no_object_moved_on_its_own", "autonomous_motion"),
    ("autonomous_motion_not_measured", "autonomous_motion"),
    ("player_has_no_renderer", "player_visible"),
    ("player_visibility_not_measured", "player_visible"),
    ("player_did_not_land", "jump_landing"),
    ("player_did_not_jump", "jump"),
    ("player_jumped_too_high", "jump"),
    ("player_jump_not_measured", "jump"),
    ("player_did_not_move_right", "movement"),
    ("player_moved_too_far", "movement"),
    ("player_movement_not_measured", "movement"),
    ("movement_path_blocked", "bidirectional"),
    ("d_did_not_move_right", "bidirectional"),
    ("d_moved_too_far", "bidirectional"),
    ("d_movement_not_measured", "bidirectional"),
    ("a_did_not_move_left", "bidirectional"),
    ("a_moved_too_far", "bidirectional"),
    ("a_movement_not_measured", "bidirectional"),
    ("idle_drift_too_large", "idle_stability"),
    ("idle_stability_not_measured", "idle_stability"),
    ("camera_did_not_follow", "camera_follow"),
    ("camera_is_player_viewpoint", "camera_follow"),
    ("camera_follow_not_measured", "camera_follow"),
    ("camera_z_changed", "camera_fixed_z"),
    ("camera_fixed_z_not_measured", "camera_fixed_z"),
    ("camera_target_null", "camera_target"),
    ("rigidbody_constraints_", "player_constraints"),
    ("left_boost_distance_too_short", "left_boost"),
    ("left_boost_not_measured", "left_boost"),
    ("boost_distance_too_short", "boost"),
    ("boost_moved_too_far", "boost"),
    ("boost_not_measured", "boost"),
    ("level_loaded_marker_missing", "level_marker"),
    ("play_screenshot_missing", "screenshot"),
    ("screenshot_file_missing", "screenshot"),
    ("runtime_errors:", "gameplay"),
    ("play_mode_not_tested", "gameplay"),
    # "런타임을 재지 못했다"도 gameplay 검사에 속한다. 매핑이 없으면 롤백 점수가
    # 이것을 프로젝트 결함으로 세고, 같은 공백을 미측정 키와 함께 두 번 센다.
    ("runtime_console_not_checked", "gameplay"),
    ("runtime_wait_missing", "gameplay"),
)

# Failure codes that carry a magnitude. Fewer errors than before is progress,
# not a new regression, so these are compared numerically instead of by string.
_COUNTED_FAILURES = ("compile_errors", "runtime_errors")


def failure_check_name(failure: str) -> str | None:
    """Canonical check a failure belongs to, or None for always-static checks.

    None means the check is never blocked (compile, scene save, asset presence),
    so its appearance is always a real regression.
    """
    if failure.startswith("component_missing:"):
        parts = failure.split(":")
        return f"components:{parts[1]}" if len(parts) > 2 else None
    for prefix, name in _FAILURE_CHECK_PREFIXES:
        if failure.startswith(prefix):
            return name
    return None


def failure_count(failure: str) -> tuple[str, int] | None:
    """Split ``compile_errors:3`` into ``("compile_errors", 3)``."""
    head, _, tail = failure.partition(":")
    if head in _COUNTED_FAILURES and tail.isdigit():
        return head, int(tail)
    return None


def fix_prompt(spec: VerificationSpec, failures: list[str], evidence: dict) -> str:
    allowed = ", ".join(spec.asset_paths) or "(요청에 명시된 기존 산출물만)"
    forbidden = (
        "LevelLoader, level JSON, StreamingAssets/Levels"
        if not spec.require_level_marker else "(없음)"
    )
    lint_guidance = []
    for failure in failures:
        if "undefined_compare_tag:" in failure:
            # 실패 문자열은 `policy_lint:undefined_compare_tag:<경로>:<태그>` 형태로
            # 오지만, lint가 직접 낸 목록에는 접두사가 없다. 양쪽을 다 받는다.
            tag = next(
                (item.rsplit(":", 1)[-1] for item in failures
                 if "undefined_compare_tag:" in item and item.count(":") >= 2),
                "Target",
            )
            # 예전 안내는 "분기 또는 OnTriggerEnter를 통째로 삭제하라"였다. 접지
            # 판정에는 맞았지만, 정적 검사가 빌더 산출 스크립트까지 보게 된 뒤로는
            # 그 분기가 사용자가 요청한 기능(코인 획득, 골인 판정)인 경우가 생긴다.
            # 삭제하면 결함 대신 기능이 사라진다. 대상 식별은 마커 컴포넌트로 옮긴다.
            lint_guidance.append(
                f"- undefined_compare_tag: 프로젝트에 `{tag}` 태그가 없어 그 "
                f"CompareTag는 런타임에 `Tag: {tag} is not defined`를 던진다. "
                "태그를 만들지 말고 마커 컴포넌트로 바꾼다 — "
                f"`public class {tag} : MonoBehaviour {{}}`를 별도 파일로 쓰고 "
                f"unity_add_component로 해당 오브젝트들에 붙인 뒤, 조건을 "
                f"`other.GetComponent<{tag}>() != null`로 바꾼다. "
                "요청이 그 분기의 동작을 요구했다면 분기를 삭제하지 말고 조건만 "
                "바꾼다. 접지 판정이라면 대신 contact.normal을 쓴다."
            )
        if "fall_respawn_check_missing:" in failure:
            lint_guidance.append(
                "- fall_respawn_check_missing: Update/FixedUpdate에서 "
                "transform.position.y 임계값을 검사해 저장된 시작 위치로 복귀시킨다."
            )
        if "legacy_input_api:" in failure:
            lint_guidance.append(
                "- legacy_input_api: UnityEngine.Input 호출을 전부 제거하고 "
                "Keyboard.current 키 상태만 사용한다."
            )
        if "idle_velocity_not_zeroed:" in failure:
            lint_guidance.append(
                "- idle_velocity_not_zeroed: 입력이 0이어도 "
                "rb.linearVelocity.x를 0으로 매 프레임 대입하고 Y 속도는 보존한다."
            )
        if "camera_z_accumulates_offset:" in failure:
            lint_guidance.append(
                "- camera_z_accumulates_offset: Start/Awake에서 fixedZ = "
                "transform.position.z를 한 번 저장한다. LateUpdate의 목표 위치는 "
                "new Vector3(target.position.x + offset.x, "
                "target.position.y + offset.y, fixedZ)로 계산한다. 현재 "
                "transform.position.z 또는 fixedZ가 든 Vector3 뒤에 `+ offset`을 "
                "붙이지 않는다."
            )
        if "rigidbody_constraints_incomplete:" in failure:
            lint_guidance.append(
                "- rigidbody_constraints_incomplete: Player Rigidbody.constraints를 "
                "FreezePositionZ | FreezeRotationX | FreezeRotationY | "
                "FreezeRotationZ로 설정한다(정수 비트값 120). 기존 필수 비트를 "
                "단일 값으로 덮어쓰지 않는다."
            )
        if failure == "scene_has_no_created_objects":
            lint_guidance.append(
                "- scene_has_no_created_objects: 활성 씬에 기본 오브젝트(Main Camera, "
                "Directional Light) 말고는 아무것도 없다. 스크립트만 쓰고 씬에 "
                "배치하지 않았거나, 다른 씬에 만들었거나, 저장 전에 씬을 바꿨을 수 "
                "있다. unity_create_gameobject로 요청한 오브젝트를 활성 씬에 만들고 "
                "unity_save_scene까지 한 뒤 unity_get_hierarchy로 확인한다."
            )
        if failure == "no_object_moved_on_its_own":
            lint_guidance.append(
                "- no_object_moved_on_its_own: 스스로 움직여야 하는 오브젝트가 "
                "입력 없는 구간에서 제자리에 있었다. 순찰 스크립트를 만들어 실제 "
                "오브젝트에 붙였는지 확인한다(스크립트만 쓰고 add_component를 "
                "빠뜨리는 경우가 많다). Update에서 transform.Translate 또는 "
                "rb.linearVelocity로 매 프레임 위치를 바꾸고, 왕복 한계에 닿으면 "
                "방향을 뒤집는다. 플레이어 입력과 무관해야 한다."
            )
        if failure == "player_has_no_renderer":
            lint_guidance.append(
                "- player_has_no_renderer: Player에 Rigidbody와 Collider만 있고 "
                "그릴 메시가 없어 화면에 보이지 않는다. 물리는 정상이므로 이동·점프 "
                "측정은 통과하지만 게임으로는 성립하지 않는다. Player를 Capsule 등 "
                "프리미티브로 다시 만들거나(MeshFilter + MeshRenderer가 함께 붙는다) "
                "기존 오브젝트에 MeshFilter와 MeshRenderer를 추가한다. 빈 "
                "GameObject에 Collider만 붙이지 않는다."
            )
        if "camera_target_null" in failure:
            lint_guidance.append(
                "- camera_target_null: Main Camera의 SideScrollerCamera.target에 "
                "기존 Player Transform을 직렬화해 연결한다. undefined tag 검색에 "
                "의존하지 않는다."
            )
        if failure.startswith("movement_path_blocked:"):
            direction = "오른쪽" if failure.endswith(":d") else "왼쪽"
            other = "왼쪽" if failure.endswith(":d") else "오른쪽"
            lint_guidance.append(
                f"- movement_path_blocked: {other} 이동은 정상인데 {direction}만 "
                f"짧게 끊겼다. 양쪽이 같은 코드를 쓰므로 **이것은 스크립트 결함이 "
                f"아니라 배치 문제다 — 입력 코드를 고치지 마라.** {direction}에 "
                "플레이어와 같은 높이로 놓인 플랫폼·벽·중복 콜라이더를 찾아 치우거나 "
                "위로 올린다. 시작 지점 좌우 각 8유닛에는 수직 장애물이 없어야 하고, "
                "플레이어는 그 평지 중앙의 겹치지 않는 위치에 있어야 한다. "
                "unity_get_hierarchy로 좌표를 확인한 뒤 unity_modify_gameobject로 "
                "옮긴다. 새 씬을 만들지 말고 스크립트도 다시 쓰지 마라."
            )
        if "a_did_not_move_left" in failure or "left_boost_distance_too_short" in failure:
            lint_guidance.append(
                "- 왼쪽 이동 실패: Player 시작 위치가 플랫폼 끝면/중복 콜라이더에 "
                "끼어 있는지 확인한다. 기존 시작 평지를 이동·확장해 시작점 좌우 "
                "각 6유닛 이상에 수직 장애물이 없는 연속 평지를 만들고, Player를 "
                "그 평지 중앙 위의 겹치지 않는 위치에 둔다. 새 씬을 만들지 않는다."
            )
        if failure in {
            "player_did_not_move_right",
            "d_did_not_move_right",
            "a_did_not_move_left",
        }:
            lint_guidance.append(
                "- 이동 입력 미전달: PlayerInput과 InputAction 바인딩이 실제로 설정되지 "
                "않은 씬에서는 OnMove(InputValue)가 호출되지 않는다. 이 경우 "
                "Keyboard.current를 null 검사한 뒤 aKey/dKey.isPressed를 매 프레임 "
                "직접 읽어 -1/0/1 축을 만들고, FixedUpdate에서 "
                "rb.linearVelocity.x = axis * moveSpeed로 대입하되 Y 속도는 보존한다. "
                "직접 Keyboard.current를 읽는 구현에는 PlayerInput, InputActionAsset, "
                "생성 controls, 별도 입력 handler가 전혀 필요하지 않으므로 추가하지 말고, "
                "연결되지 않은 해당 컴포넌트가 이미 있으면 제거한다. "
                "legacy UnityEngine.Input API는 사용하지 않는다."
            )
        if "moved_too_far" in failure:
            lint_guidance.append(
                "- 이동 거리 과다: 매 FixedUpdate에서 AddForce(..., ForceMode.Impulse)를 "
                "호출하면 속도가 누적돼 폭주한다. 지속 입력은 속도를 직접 설정하거나"
                "(rb.linearVelocity = new Vector3(input * moveSpeed, rb.linearVelocity.y, 0)) "
                "ForceMode.Force로 바꾸고, 최대 속도를 제한한다."
            )
        if failure == "camera_is_player_viewpoint":
            lint_guidance.append(
                "- 카메라가 관찰이 아니라 1인칭 시점이 됐다: Main Camera가 Player와 "
                "같은 자리에 있거나 Player의 자식이라 플레이어가 화면에 보이지 않는다. "
                "카메라를 Player의 자식으로 두지 말고 별도 오브젝트로 유지한 뒤, "
                "LateUpdate에서 target.position + offset으로 따라가게 한다. 2.5D "
                "측면 뷰라면 offset은 Z로 물러나고 Y로 올라간 값이어야 한다"
                "(예: new Vector3(0, 2, -10)). 카메라와 Player의 거리가 "
                f"{_CAMERA_MIN_GAP}유닛 미만이면 관찰로 인정하지 않는다."
            )
        if failure == "boost_moved_too_far":
            # 실측: 0.5초에 140유닛(일반 이동의 56배). shift를 누르고 있는 동안
            # 매 FixedUpdate가 impulse를 다시 줘서 속도가 누적됐다.
            lint_guidance.append(
                "- 부스트 과다: shift를 누르고 있는 동안 매 FixedUpdate에서 impulse를 "
                "다시 주면 속도가 누적돼 플레이어가 화면 밖으로 날아간다. 대시는 "
                "속도 대입으로 표현하고(rb.linearVelocity = new Vector3(axis * "
                "moveSpeed * boostMultiplier, rb.linearVelocity.y, 0f)), impulse로 "
                "구현할 거라면 점프와 같은 rising edge 래치로 한 번만 적용한 뒤 "
                "지속 시간이 끝나면 속도를 원래대로 되돌린다. 호스트는 부스트 거리가 "
                f"일반 이동의 {spec.boost_max_ratio}배를 넘으면 실패로 판정한다."
            )
        if failure in {"boost_distance_too_short", "left_boost_distance_too_short"}:
            # 실제 실행에서 본 형태: 대시를 AddForce impulse로 주는데 같은
            # FixedUpdate가 rb.linearVelocity를 통째로 대입해 그 impulse를 지웠다.
            # 측정 비율이 1.04로 나왔고 repair 2회가 원인을 못 짚었다.
            lint_guidance.append(
                "- 부스트가 측정되지 않음: 매 FixedUpdate에서 "
                "rb.linearVelocity = new Vector3(axis * moveSpeed, ...)처럼 수평 속도를 "
                "통째로 대입하면, 같은 프레임에 AddForce(..., ForceMode.Impulse)로 준 "
                "대시 속도가 곧바로 덮여 사라진다. 대시를 속도 대입 자체에 반영해라 — "
                "float speed = boosting ? moveSpeed * boostMultiplier : moveSpeed; "
                "rb.linearVelocity = new Vector3(axis * speed, rb.linearVelocity.y, 0f); "
                "boosting은 Keyboard.current.leftShiftKey.isPressed로 읽는다. "
                "호스트는 이동키와 leftShift를 함께 0.5초 누른 거리를 같은 시간 일반 "
                f"이동 거리와 비교해 {spec.boost_min_ratio}배 이상일 때만 통과시킨다."
            )
            # 실측: Platform이 x=3~7, 플레이어 높이에 놓여 오른쪽 이동이 2.06에서
            # 멈췄다(반대 방향은 4.96). 막힌 방향에서는 부스트가 빨라져도 거리가
            # 같으므로 비율이 1에 붙고, 코드를 고쳐서는 절대 통과할 수 없다.
            lint_guidance.append(
                "- 부스트 이전에 배치를 확인해라: 측정값에서 한쪽 이동 거리가 반대쪽보다 "
                "현저히 짧으면 그 방향에 장애물이 있다는 뜻이다. 호스트는 시작 지점에서 "
                "정해진 시간 동안 이동한 거리로 판정하므로, 막힌 방향에서는 부스트가 "
                "동작해도 비율이 1에 가깝게 나온다. Player 시작 지점 좌우로 각 8유닛 "
                "이상은 같은 높이에 막는 물체가 없어야 한다. 플랫폼은 그 바깥이나 "
                "위쪽에 둔다."
            )
        if failure == "player_did_not_jump":
            lint_guidance.append(
                "- 점프 실패: CheckGrounded Raycast가 안정 착지 위치에서 확실히 "
                "Collider를 맞히도록 거리/시작점을 Collider.bounds 기반으로 고치거나, "
                "OnCollisionEnter의 contact.normal이 위를 향할 때 접지로 판정한다. "
                f"{_JUMP_EDGE_GUIDANCE}"
            )
            # 실측된 형태: 층을 같은 X에 3유닛 간격으로 쌓아 Player 머리 바로 위가
            # 다음 층 바닥이었다. 스크립트는 정상인데 상승량이 정확히 0.0이었다.
            lint_guidance.append(
                "- 점프 실패가 코드가 아니라 배치일 수 있다: 상승량이 정확히 0이면 "
                "Player 머리 위에 천장이 있는지 먼저 확인해라. 여러 층을 같은 X 위에 "
                "쌓으면 아래층 Player의 머리 바로 위가 위층 바닥이 되어 물리적으로 "
                "뛸 수 없다. Player 콜라이더 상단에서 위로 최소 3유닛은 비워야 하며, "
                "층은 수직으로만 쌓지 말고 X 방향으로 어긋나게(계단처럼) 배치한다. "
                "Player는 최하층 평지 위, 위층 바닥과 겹치지 않는 X 위치에 둔다."
            )
        if failure == "player_jumped_too_high":
            lint_guidance.append(
                "- 점프 과다: 키를 누르고 있는 동안 래치가 계속 다시 서면 "
                "AddForce(..., ForceMode.Impulse)가 여러 물리 스텝에 걸쳐 중첩돼 "
                "발사 속도가 배로 커진다. 한 번 누를 때 impulse는 정확히 한 번만 "
                f"적용돼야 한다. {_JUMP_EDGE_GUIDANCE}"
            )
    lint_section = "\n".join(dict.fromkeys(lint_guidance)) or "- 해당 없음"
    return f"""[독립 검증 실패 자동 수정 단계]
원래 요청:
{spec.request}

호스트 검증 실패 항목:
{json.dumps(failures, ensure_ascii=False, indent=2)}

수집된 측정값:
{json.dumps(evidence, ensure_ascii=False, indent=2, default=str)}

수정 허용 범위:
{allowed}

금지 범위:
{forbidden}

정적 정책 실패의 결정적 수정법:
{lint_section}

실패 항목만 고쳐라. 기존 파일과 오브젝트를 먼저 조회해 재사용하고 성공한 산출물을
재생성하지 마라. 편집은 Play Mode를 끝낸 뒤 수행하고 저장/컴파일 확인까지 마쳐라.
완료 판정은 다음 fresh 검증 단계가 하므로 수정 결과를 스스로 완료라고 선언하지 마라."""


def write_receipt(root_dir: str, spec: VerificationSpec, status: str, evidence: dict,
                  failures: list[str], attempts: list[dict], elapsed_seconds: float,
                  build_success: bool | None = None,
                  check_report: dict | None = None) -> str:
    now = datetime.now().astimezone()
    day = os.path.join(os.path.abspath(root_dir), now.strftime("%Y"), now.strftime("%m"),
                       now.strftime("%d"))
    os.makedirs(day, exist_ok=True)
    path = os.path.join(
        day, f"{now.strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{uuid.uuid4().hex[:10]}.json"
    )
    payload = {
        "version": VERSION,
        "timestamp": now.isoformat(timespec="milliseconds"),
        "status": status,
        "request": spec.request,
        "build_stage_success": build_success,
        # Requested vs measured makes an empty verification visible at a glance
        # instead of hiding behind an empty failure list.
        **(check_report or {
            "requested_checks": spec.requested_checks(),
            "measured_checks": [],
            "skipped_checks": spec.requested_checks(),
        }),
        # Requirements the request stated that no check will ever look at. A
        # receipt that says `verified` while this list is non-empty verified a
        # subset, and the reader has to be able to see which part.
        "unmapped_requirements": spec.unmapped_requirements(),
        "spec": asdict(spec),
        "evidence": evidence,
        "failures": failures,
        "attempts": attempts,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return path
