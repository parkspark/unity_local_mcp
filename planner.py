"""호스트 측 플래너: 큰 요청을 마일스톤으로 분해한다.

모델은 계획을 '제안'만 하고(별도 chat 호출, Ollama structured output으로 JSON 강제),
호스트가 스키마 검증·정규화·완료 판정을 결정적으로 수행한다. 어떤 단계에서든
실패하면 None을 반환해 기존 단일 ReAct 루프로 강등된다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import config

# ------------------------------------------------------------------ 판별

_LEVEL_COUNT = re.compile(r"(레벨|스테이지|level|stage)\s*\d|\d\s*(개의?\s*)?(레벨|스테이지|levels?|stages?)", re.I)
_GENRE = re.compile(r"플랫포머|플랫폼\s*게임|슈팅|퍼즐|테트리스|소코반|rpg|로그라이크|platformer|shooter|puzzle|tetris|sokoban|게임", re.I)
_BUILD_VERB = re.compile(r"만들|구현|제작|개발|create|build|implement|make", re.I)
_EXPLICIT_LEVEL_SYSTEM = re.compile(
    r"(레벨|스테이지|level|stage)\s*\d|\d\s*(개의?\s*)?(레벨|스테이지|levels?|stages?)"
    r"|level\s*loader|데이터\s*주도|data[- ]driven|level\s*json|레벨\s*json",
    re.I,
)
_LEVEL_MILESTONE = re.compile(
    r"level(loader|data)?|레벨\s*(로더|데이터|json)|스테이지\s*(로더|데이터|json)|"
    r"unity_(install_level_loader|write_level)|StreamingAssets/Levels/|\.json",
    re.I,
)


def looks_large(request: str) -> bool:
    """플래닝이 도움이 될 큰 요청인지에 대한 결정적 휴리스틱.

    오판 비용은 낮다: false negative는 기존 동작(무해), false positive는
    플래닝 호출 1회 후 mode=single로 강등.
    """
    if _LEVEL_COUNT.search(request) and _BUILD_VERB.search(request):
        return True
    if _GENRE.search(request) and _BUILD_VERB.search(request):
        return True
    if len(request) > 200 and _BUILD_VERB.search(request):
        return True
    return False


# 검증 절차·합격 조건 블록. **검증은 호스트가 한다** — 이 절들은 빌더가 읽어야 하는
# 것이지 계획의 재료가 아니다. 그런데 계획 호출에 그대로 실려 가면 모델이 요청 전체를
# "하나의 선형 절차"로 읽고 `mode: "single"`로 답한다.
#
# 실측(플랫포머 MVP 프롬프트 2802자, temperature 0.1, 4회씩):
#     원문 그대로     single/마일스톤1  4/4
#     검증 섹션 제거  plan/마일스톤5~6  4/4
# 잘라낸 길이별 시험에서도 2200자까지는 plan 3/3이고 전문에서만 single 3/3이었다 —
# 길이가 아니라 꼬리의 내용이 원인이다.
_VERIFICATION_SECTION = re.compile(
    r"^\[[^\]\n]*(?:검증|합격|verif|pass condition)[^\]\n]*\][^\[]*",
    re.I | re.M,
)
# 이 아래로 줄어들면 계획을 세울 재료가 남지 않는다. 그때는 원문을 그대로 쓴다.
_MIN_PLAN_INPUT = 120


def plan_input(request: str) -> str:
    """계획 호출에만 쓰는 요청 본문. 빌더에게 가는 요청은 손대지 않는다."""
    stripped = _VERIFICATION_SECTION.sub("", request).strip()
    return stripped if len(stripped) >= _MIN_PLAN_INPUT else request


def _prune_unrequested_level_system(plan: "Plan", request: str, on_warn) -> "Plan | None":
    """Do not let a generic MVP platformer turn into an invented level-loader project."""
    if _EXPLICIT_LEVEL_SYSTEM.search(request):
        return plan
    kept = []
    removed = 0
    for milestone in plan.milestones:
        text = " ".join([milestone.title, milestone.goal, *milestone.deliverables])
        if _LEVEL_MILESTONE.search(text):
            removed += 1
            continue
        kept.append(milestone)
    if removed:
        on_warn(f"요청에 없는 데이터 주도 레벨 단계 {removed}개를 계획에서 제거했습니다.")
    if len(kept) < 2:
        return None
    return Plan(request=plan.request, milestones=kept)


# ------------------------------------------------------------------ 데이터

VERIFY_KINDS = ("compile", "scene", "play", "screenshot", "input")


@dataclass
class Milestone:
    id: str
    title: str
    goal: str
    deliverables: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    max_iters: int = 0

    def __post_init__(self):
        if not self.max_iters:
            self.max_iters = config.MILESTONE_MAX_ITERS


@dataclass
class Plan:
    request: str
    milestones: list[Milestone]


PLAN_JSON_SCHEMA = {
    "type": "object",
    "required": ["mode"],
    "properties": {
        "mode": {"enum": ["single", "plan"]},
        "milestones": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["title", "goal", "deliverables", "verify"],
                "properties": {
                    "title": {"type": "string", "maxLength": 60},
                    "goal": {"type": "string", "maxLength": 700},
                    "deliverables": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "verify": {"type": "array", "items": {"enum": list(VERIFY_KINDS)}},
                },
            },
        },
    },
}

PLANNER_PROMPT = """\
You split one Unity Editor request into sequential milestones for a small agent.
Respond with JSON only, matching the given schema. No prose.

Decide "mode":
- "single": the request is one small task (create an object, tweak a property, a question).
- "plan": the request builds something with multiple parts (a game, levels, several systems).

Scope rule:
- Do not invent stages, level JSON files, or LevelLoader for a generic MVP or a
  single-scene platformer. Add the data-driven level system only when the user
  explicitly asks for multiple levels/stages, level JSON, LevelLoader, or a
  data-driven level workflow. The host removes accidental level milestones too.

If mode is "plan", write 2-6 milestones. Rules for each milestone:
- "goal": a self-contained instruction the executor can follow without reading the other milestones. Write it in the user's language. Be specific about file paths and object names.
- "deliverables": project-relative Assets/... file paths this milestone must create (scripts .cs, scenes .unity, levels .json). Empty list if none.
- "verify": subset of ["compile","scene","play","screenshot","input"] — compile: C# was written; scene: scene objects changed; play: must run in play mode; screenshot: user asked for a capture; input: gameplay must be verified with simulated key input.

Environment facts (do not contradict):
- Data-driven levels: install the canonical loader with unity_install_level_loader (do NOT write a custom loader), then write levels as JSON with unity_write_level to Assets/StreamingAssets/Levels/levelN.json. The player object must be named "Player".
- The loader builds a 3D world. Player movement must use Rigidbody and 3D Collider/Collision APIs, never Rigidbody2D or other 2D physics APIs.
- Scripts go under Assets/Scripts/, scenes under Assets/Scenes/.
- A good split for "a platformer with 3 levels": ① install the canonical
  level loader + write player movement script + compile check ② create scene,
  LevelLoader object, Player, camera, save ③ write level1-3 JSON files ④ play
  + simulated input verification (+ screenshot if asked).
"""


# ------------------------------------------------------------------ 검증/정규화

def _normalise_path(p: str) -> str:
    return str(p or "").replace("\\", "/").strip().lstrip("/")


def validate_plan(data) -> Plan | None:
    """모델이 낸 계획 JSON을 정규화. 쓸 수 없는 계획이면 None(단일 루프 강등)."""
    if not isinstance(data, dict) or data.get("mode") != "plan":
        return None
    raw = data.get("milestones")
    if not isinstance(raw, list):
        return None
    milestones: list[Milestone] = []
    for item in raw[: config.PLAN_MAX_MILESTONES]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:60]
        goal = str(item.get("goal") or "").strip()[:700]
        if not title or not goal:
            continue
        deliverables = []
        for d in item.get("deliverables") or []:
            norm = _normalise_path(d)
            if norm.startswith("Assets/") and ".." not in norm:
                deliverables.append(norm)
        verify = [v for v in (item.get("verify") or []) if v in VERIFY_KINDS]
        milestones.append(Milestone(
            id=f"m{len(milestones) + 1}",
            title=title,
            goal=goal,
            deliverables=deliverables[:6],
            verify=verify,
        ))
    if len(milestones) < 2:
        return None
    return Plan(request="", milestones=milestones)


def _extract_json(text: str) -> dict | None:
    """코드펜스/잡문 속에서 첫 JSON 오브젝트를 건져낸다."""
    from mcp_client import _lenient_json
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = _lenient_json(text[start:end + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


async def make_plan(
    client, model: str, request: str, on_warn=lambda m: None,
    on_reason=lambda code, detail=None: None,
) -> Plan | None:
    """도구 없는 별도 호출로 계획 JSON을 받는다. 실패 시 None(단일 루프 강등).

    **강등에는 반드시 이유가 남는다.** `mode: "single"`을 모델이 고르는 경로가
    유일하게 조용했고, 그래서 큰 요청이 단일 루프로 떨어져도 로그에 흔적이 없었다
    (`20260809_123343` — 플랫포머 한 스테이지 요청이 계획 없이 돌았다).
    """
    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content": plan_input(request)},
    ]
    for attempt, temperature in enumerate((0.1, 0.0)):
        try:
            resp = await client.chat(
                model=model,
                messages=messages,
                format=PLAN_JSON_SCHEMA,  # Ollama structured output: 디코딩 수준 제약
                keep_alive=config.KEEP_ALIVE,
                think=config.reasoning_option(),
                options=config.model_options(model=model, temperature=temperature),
            )
        except Exception as e:
            on_warn(f"플래닝 호출 실패({type(e).__name__}: {e}) — 단일 루프로 진행합니다.")
            on_reason("planner_call_failed", f"{type(e).__name__}: {e}")
            return None
        content = resp.message.content or ""
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            data = _extract_json(content)
        if isinstance(data, dict):
            if data.get("mode") == "single":
                # 모델의 `mode` 라벨은 이 저장소가 가장 못 믿는 필드다 — 마일스톤을
                # 여섯 개나 채워 놓고 `single`이라고 답한 표본이 있다. 이 지점에
                # 왔다는 것은 호스트의 `looks_large`가 이미 큰 요청으로 판단했다는
                # 뜻이므로, **라벨보다 내용을 본다**(planner의 설계 원칙 그대로).
                forced = validate_plan({**data, "mode": "plan"})
                if forced is not None:
                    on_warn(
                        "모델은 단일 작업이라 했지만 마일스톤 "
                        f"{len(forced.milestones)}개를 제시해 계획으로 진행합니다."
                    )
                    on_reason("mode_overridden_by_content")
                    forced.request = request
                    return _prune_unrequested_level_system(forced, request, on_warn)
                on_warn(
                    "모델이 이 요청을 단일 작업으로 판단했습니다 — 계획 없이 진행합니다."
                )
                on_reason("model_chose_single")
                return None
            plan = validate_plan(data)
            if plan is not None:
                plan.request = request
                pruned = _prune_unrequested_level_system(plan, request, on_warn)
                if pruned is None:
                    on_warn("남은 마일스톤이 2개 미만이라 단일 루프로 진행합니다.")
                    on_reason("pruned_below_minimum")
                return pruned
        if attempt == 0:
            on_warn("계획 JSON이 유효하지 않아 한 번 더 시도합니다.")
    on_warn("계획을 만들지 못했습니다 — 단일 루프로 진행합니다.")
    on_reason("invalid_plan_json")
    return None


# ------------------------------------------------------------------ 산출물 대장

_LEDGER_CAP = 10  # 카테고리별 항목 상한 (마일스톤 프롬프트 크기 고정)


class ArtifactLedger:
    """마일스톤 간 전달되는 산출물 요약. 성공한 도구 결과에서만 결정적으로 수집."""

    def __init__(self):
        self.scripts: list[str] = []
        self.levels: list[str] = []
        self.scenes: list[str] = []
        self.objects: list[str] = []
        self.screenshots: list[str] = []
        self.done: list[tuple[str, bool | None, str]] = []  # (title, ok; None=not started, note)

    @staticmethod
    def _successful(result: str) -> bool:
        try:
            data, _end = json.JSONDecoder().raw_decode(str(result).lstrip())
            return isinstance(data, dict) and data.get("status") == "ok"
        except (TypeError, ValueError, AttributeError):
            return False

    def _add(self, bucket: list[str], value: str):
        if value and value not in bucket and len(bucket) < _LEDGER_CAP:
            bucket.append(value)

    def observe(self, name: str, args: dict, result: str):
        if not self._successful(result):
            return
        if name in ("unity_write_script", "unity_install_level_loader"):
            path = _normalise_path(args.get("path", "")) or "Assets/Scripts/LevelLoader.cs"
            self._add(self.scripts, path)
        elif name == "unity_write_level":
            self._add(self.levels, _normalise_path(args.get("path", "")))
        elif name in ("unity_create_scene", "unity_open_scene"):
            self._add(self.scenes, _normalise_path(args.get("path", "")))
        elif name in ("unity_create_gameobject", "unity_add_component"):
            try:
                self._add(self.objects, json.loads(result)["result"]["path"])
            except (KeyError, TypeError, ValueError):
                pass
        elif name == "unity_create_gameobjects":
            try:
                for p in json.loads(result)["result"].get("paths", []):
                    self._add(self.objects, str(p))
            except (KeyError, TypeError, ValueError):
                pass
        elif name == "unity_screenshot":
            try:
                self._add(self.screenshots, json.loads(result)["result"]["path"])
            except (KeyError, TypeError, ValueError):
                pass

    def milestone_done(self, title: str, ok: bool, note: str = ""):
        self.done.append((title, ok, note[:200]))

    def milestone_pending(self, title: str):
        self.done.append((title, None, "미착수"))

    def summary(self) -> str:
        lines = []
        if self.scripts:
            lines.append("- scripts: " + ", ".join(self.scripts))
        if self.levels:
            lines.append("- levels: " + ", ".join(self.levels))
        if self.scenes:
            lines.append("- scenes: " + ", ".join(self.scenes))
        if self.objects:
            lines.append("- scene objects: " + ", ".join(self.objects))
        if self.screenshots:
            lines.append("- screenshots: " + ", ".join(self.screenshots))
        return "\n".join(lines) if lines else "- (none yet)"

    def report(self) -> str:
        lines = ["작업 결과:"]
        for title, ok, note in self.done:
            mark = "✓" if ok is True else "✗" if ok is False else "·"
            lines.append(f"- {mark} {title}" + (f" ({note})" if note and ok is not True else ""))
        lines.append("")
        lines.append("산출물:")
        lines.append(self.summary())
        return "\n".join(lines)
