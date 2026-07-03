# Unity Local Agent

# 시연
![alt text](data/0703_unity_mcp1.gif)


로컬 LLM(Ollama)으로 Unity Editor를 제어하는 채팅 CLI.
기존 [unity_mcp](../unity_mcp) MCP 서버와 Unity 브리지를 **수정 없이** 재사용하고,
Claude Code의 자리를 로컬 모델이 대신한다.

```
[사용자] ⇄ CLI 채팅 (main.py)
              │
        Agent 루프 (agent.py) ⇄ Ollama (qwen3-coder:30b, localhost:11434)
              │  tool_calls
        MCP 클라이언트 (mcp_client.py)
              │  stdio (uv run server.py 자식 프로세스)
        unity_mcp/server.py
              │  TCP 127.0.0.1:8722 JSON-lines
        Unity Editor (UnityMcpBridge.cs)
```

## 요구 사항

- Windows + NVIDIA GPU (RTX 5090 32GB 기준으로 튜닝됨)
- [Ollama](https://ollama.com/download) ≥ 0.9 — qwen3-coder tool-call 필요
- [uv](https://docs.astral.sh/uv/)
- 옆 폴더의 `unity_mcp` 프로젝트 + Unity 프로젝트에 `UnityMcpBridge.cs` 설치
  (Unity 콘솔에 `[McpBridge] Listening`이 보여야 함)

## 설치

```bash
# 1. 모델 받기 (~19GB)
ollama pull qwen3-coder:30b

# 2. (권장) KV 캐시 최적화 — 32k 컨텍스트도 GPU에 여유 있게
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
# 설정 후 Ollama 재시작 필요

# 3. 의존성 설치
cd unity_local_mcp
uv sync

# 4. (선택) 스크린샷 분석용 비전 모델
ollama pull qwen2.5vl:7b
```

## 사용

Unity Editor를 열어 둔 상태에서:

```bash
uv run main.py            # 채팅 시작
uv run main.py --vision   # /look 스크린샷 분석 활성화
```

```
연결됨: 18 tools · qwen3-coder:30b · ctx 32768
Unity 6000.5.2f1 · My project

you> 바닥 평면 만들고 그 위에 빨간 큐브 3개를 x축으로 2씩 띄워 배치해줘
→ unity_create_gameobject {"name": "Floor", "primitive": "Plane"}
← {"status":"ok",...}
...
```

### 명령어

| 명령 | 동작 |
|---|---|
| `/reset` | 대화 초기화 |
| `/tools` | 사용 가능한 도구 목록 |
| `/model <이름>` | 모델 변경 (예: `/model qwen3:32b`) |
| `/last` | 마지막 도구 결과 원본(절단 전) 보기 |
| `/look [질문]` | 마지막 스크린샷을 비전 모델로 분석 (`--vision` 필요) |
| `/quit` | 종료 |

### 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `UNITY_AGENT_MODEL` | `qwen3-coder:30b` | 사용할 Ollama 모델 |
| `UNITY_AGENT_NUM_CTX` | `32768` | 컨텍스트 길이. VRAM 부족(CPU 분할) 시 16384로 |
| `UNITY_AGENT_MAX_ITERS` | `15` | 한 턴의 최대 도구 호출 반복 |
| `UNITY_AGENT_STREAM` | `1` | 스트리밍 중 tool_calls가 안 오면 `0` |
| `UNITY_MCP_DIR` | `..\unity_mcp` 절대경로 | MCP 서버 위치 |
| `UNITY_AGENT_AUTO_OPEN` | `1` | 스크린샷 자동 열기 |

## 트러블슈팅

- **모델이 도구를 "잊거나" 시스템 프롬프트를 무시함** — 컨텍스트 오버플로.
  Ollama 서버 로그에 `truncating input prompt`가 있으면 확정. `num_ctx`를 늘리거나 `/reset`.
- **응답이 갑자기 10배 느려짐** — 모델이 CPU로 분할됨. `ollama ps`가 `100% GPU`인지 확인.
  아니면 `UNITY_AGENT_NUM_CTX=16384`로 낮추기.
- **`<function=...>` 같은 텍스트가 답변에 섞임** — Ollama가 tool-call을 파싱하지 못함.
  Ollama를 최신 버전으로 업데이트.
- **Unity bridge unreachable** — Unity Editor가 열려 있고 콘솔에 `[McpBridge] Listening`이
  있는지 확인. 플레이 모드 전환/스크립트 컴파일 직후에는 몇 초 기다렸다 재시도.
- **첫 응답이 20~40초 느림** — 모델 로딩. 이후엔 `keep_alive=30m`으로 메모리에 유지됨.
- **한글 깨짐** — Windows Terminal 사용 권장.
- **비정상 종료 후 `uv`/`python` 프로세스가 남음** — 작업 관리자에서 정리.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `config.py` | 모델·컨텍스트·한도 등 설정 |
| `mcp_client.py` | MCP stdio 세션, 도구 스키마 → Ollama 형식 변환, 인자 보정·결과 절단 |
| `agent.py` | 시스템 프롬프트 + tool-call 루프 |
| `main.py` | REPL, 슬래시 명령, 스크린샷 처리 |
| `mcp_server.log` | MCP 서버 stderr 로그 (실행 시 생성) |


# 버전
ver 1.0 - 프로토타입 MVP