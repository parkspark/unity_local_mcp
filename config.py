"""에이전트 설정. 환경변수로 오버라이드 가능."""

import os

# Ollama
MODEL = os.environ.get("UNITY_AGENT_MODEL", "qwen3-coder:30b")
VISION_MODEL = os.environ.get("UNITY_AGENT_VISION_MODEL", "qwen2.5vl:7b")
NUM_CTX = int(os.environ.get("UNITY_AGENT_NUM_CTX", "32768"))
TEMPERATURE = float(os.environ.get("UNITY_AGENT_TEMPERATURE", "0.2"))
KEEP_ALIVE = os.environ.get("UNITY_AGENT_KEEP_ALIVE", "30m")
# 스트리밍 중 tool_calls가 안 오는 Ollama 버전이면 False로
STREAM = os.environ.get("UNITY_AGENT_STREAM", "1") != "0"

# 에이전트 루프
MAX_ITERS = int(os.environ.get("UNITY_AGENT_MAX_ITERS", "15"))
TRUNCATE_CHARS = int(os.environ.get("UNITY_AGENT_TRUNCATE_CHARS", "8000"))
# 히스토리 트리밍 기준: 추정 토큰이 num_ctx의 이 비율을 넘으면 오래된 대화 삭제
HISTORY_BUDGET_RATIO = 0.7

# 기존 Unity MCP 서버 위치 (수정 없이 재사용)
UNITY_MCP_DIR = os.environ.get(
    "UNITY_MCP_DIR", r"C:\Users\park\Desktop\dev_tool\unity_mcp"
)

# 스크린샷 PNG를 자동으로 열지 여부
AUTO_OPEN_SCREENSHOT = os.environ.get("UNITY_AGENT_AUTO_OPEN", "1") != "0"
