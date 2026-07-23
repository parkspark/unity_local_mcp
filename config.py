"""에이전트 설정. 환경변수로 오버라이드 가능."""

import os

from version import __version__

# Ollama
MODEL = os.environ.get("UNITY_AGENT_MODEL", "qwen3-coder:30b")
# qwen2.5vl:32b is installed on the target workstation. Keep this overrideable
# for smaller machines, but do not default to a model that is absent locally.
VISION_MODEL = os.environ.get("UNITY_AGENT_VISION_MODEL", "qwen2.5vl:32b")
NUM_CTX = int(os.environ.get("UNITY_AGENT_NUM_CTX", "32768"))
TEMPERATURE = float(os.environ.get("UNITY_AGENT_TEMPERATURE", "0.2"))
KEEP_ALIVE = os.environ.get("UNITY_AGENT_KEEP_ALIVE", "30m")
# 스트리밍 중 tool_calls가 안 오는 Ollama 버전이면 False로
STREAM = os.environ.get("UNITY_AGENT_STREAM", "1") != "0"

# 에이전트 루프
MAX_ITERS = int(os.environ.get("UNITY_AGENT_MAX_ITERS", "15"))
TRUNCATE_CHARS = int(os.environ.get("UNITY_AGENT_TRUNCATE_CHARS", "4000"))
# 한 턴에서 같은 툴이 이 횟수만큼 호출되면 배치/스크립트 사용을 권고. 0이면 끔.
LOOP_GUARD_THRESHOLD = int(os.environ.get("UNITY_AGENT_LOOP_GUARD", "4"))
# 히스토리 트리밍 기준: 추정 토큰이 num_ctx의 이 비율을 넘으면 오래된 대화 삭제
HISTORY_BUDGET_RATIO = 0.7

# 기존 Unity MCP 서버 위치 (수정 없이 재사용)
UNITY_MCP_DIR = os.environ.get(
    "UNITY_MCP_DIR", r"C:\Users\park\Desktop\dev_tool\unity_mcp"
)

# Unity 프로젝트 경로 (스크립트 쓰기 + 브리지 포트 파일 읽기용).
# 비우면 스크립트 쓰기는 unity_ping의 projectPath로 자동 발견하지만,
# 포트 파일은 연결 전에 필요하므로 지정해두는 것을 권장.
UNITY_PROJECT_DIR = os.environ.get("UNITY_PROJECT_DIR", r"C:\Users\park\My project")

# 스크린샷 PNG를 자동으로 열지 여부
AUTO_OPEN_SCREENSHOT = os.environ.get("UNITY_AGENT_AUTO_OPEN", "1") != "0"
# When enabled, a screenshot tool result is analysed by VISION_MODEL locally and
# the finding is returned to the coding model in the same task loop. It is off
# by default because the 30B coder and 32B vision model should be run
# sequentially on a 32 GB VRAM card.
AUTO_VISION = os.environ.get("UNITY_AGENT_AUTO_VISION", "0") != "0"

# 컴파일 대기 시 Unity 창에 잠깐 포커스를 줄지 여부.
# Unity는 백그라운드에 있으면 스크립트 컴파일을 미루므로 기본 켬.
FOCUS_UNITY_ON_COMPILE = os.environ.get("UNITY_AGENT_FOCUS_COMPILE", "1") != "0"

# "Script Updating Consent" 모달(구식 API 자동 변환 동의)을 자동으로 수락할지.
# 모달이 뜨면 에디터 메인 스레드가 멈춰 브리지가 마비되므로 기본 켬.
AUTO_CONSENT = os.environ.get("UNITY_AGENT_AUTO_CONSENT", "1") != "0"

# ---- v1.7: 플래너 / 입력 시뮬레이션 ----

# 큰 요청을 마일스톤으로 분해할지: auto(휴리스틱), always, off
PLANNER = os.environ.get("UNITY_AGENT_PLANNER", "auto")
PLAN_MAX_MILESTONES = int(os.environ.get("UNITY_AGENT_PLAN_MAX", "6"))
MILESTONE_MAX_ITERS = int(os.environ.get("UNITY_AGENT_MILESTONE_ITERS", "12"))
MILESTONE_RETRIES = int(os.environ.get("UNITY_AGENT_MILESTONE_RETRIES", "1"))
# 계획 전체의 iteration 총량 안전판
PLAN_MAX_TOTAL_ITERS = int(os.environ.get("UNITY_AGENT_PLAN_TOTAL_ITERS", "60"))

# unity_send_key 직전에 Unity 창에 포커스를 줄지. Unity가 최소화/백그라운드면
# 플레이어 루프 전체가 멈춰 입력이 게임에 닿지 않는 것을 실측으로 확인 — 기본 켬.
FOCUS_UNITY_ON_INPUT = os.environ.get("UNITY_AGENT_FOCUS_INPUT", "1") != "0"

# ---- v1.8: 실행 로그 ----

# Agent.run_turn() 단위로 사람이 읽는 .log와 기계 분석용 .jsonl을 함께 저장한다.
# CLI뿐 아니라 e2e_v17_run.py 같은 프로그램 호출도 같은 공통 경로를 사용한다.
RUN_LOGS = os.environ.get("UNITY_AGENT_RUN_LOGS", "1") != "0"
RUN_LOG_DIR = os.path.abspath(os.environ.get(
    "UNITY_AGENT_RUN_LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "runs"),
))

# ---- v1.8.1: MCP 감사 로그 / 검증 전용 모드 ----

# Agent를 거치지 않는 직접 UnityTools.call()까지 빠짐없이 JSONL로 기록한다.
MCP_AUDIT_LOGS = os.environ.get("UNITY_MCP_AUDIT_LOGS", "1") != "0"
MCP_AUDIT_LOG_DIR = os.path.abspath(os.environ.get(
    "UNITY_MCP_AUDIT_LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "mcp"),
))

# full: 모든 도구, verify: 조회·플레이·입력·스크린샷 도구만 노출/허용.
TOOL_MODE = os.environ.get("UNITY_AGENT_TOOL_MODE", "full").strip().lower()
if TOOL_MODE not in {"full", "verify"}:
    TOOL_MODE = "full"

# ---- v1.9: 독립 검증 오케스트레이션 ----

# 모델의 자기 보고와 별개로, 새 컨텍스트의 검증 단계가 실제 Unity 측정값을
# 수집해야만 요청을 완료로 판정한다. 무한 반복 대신 시간 예산 + 실패 정체 감지로
# 오래 실행되는 작업도 안전하게 종료한다.
VERIFY_ORCHESTRATION = os.environ.get("UNITY_AGENT_VERIFY", "1") != "0"
FIX_MAX_CYCLES = int(os.environ.get("UNITY_AGENT_FIX_CYCLES", "3"))
FIX_MAX_ITERS = int(os.environ.get("UNITY_AGENT_FIX_ITERS", "20"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("UNITY_AGENT_TASK_TIMEOUT", "1800"))
NO_PROGRESS_LIMIT = int(os.environ.get("UNITY_AGENT_NO_PROGRESS_LIMIT", "2"))
VERIFICATION_RECEIPT_DIR = os.path.abspath(os.environ.get(
    "UNITY_AGENT_RECEIPT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "receipts"),
))

# ---- v1.10: deterministic preflight / recovery ----

VERSION = __version__
# strict: conflicting scene paths stop before mutation.
# acceptance: the single scene path in the acceptance section wins.
SCENE_PATH_POLICY = os.environ.get(
    "UNITY_AGENT_SCENE_PATH_POLICY", "strict"
).strip().lower()
if SCENE_PATH_POLICY not in {"strict", "acceptance"}:
    SCENE_PATH_POLICY = "strict"

# Read-only inspection of an existing project script can be enabled without
# granting delete access. This is useful for conversational diagnose/fix tasks
# where naming every existing script is unnecessarily repetitive.
ALLOW_UNSCOPED_SCRIPT_READ = os.environ.get(
    "UNITY_AGENT_ALLOW_UNSCOPED_SCRIPT_READ", "0"
).strip().lower() in {"1", "true", "yes", "on"}

BRIDGE_RECOVERY_TIMEOUT_SECONDS = float(os.environ.get(
    "UNITY_AGENT_BRIDGE_RECOVERY_TIMEOUT", "120"
))
BRIDGE_RECOVERY_POLL_SECONDS = float(os.environ.get(
    "UNITY_AGENT_BRIDGE_RECOVERY_POLL", "1"
))
