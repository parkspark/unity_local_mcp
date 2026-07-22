# Unity Local Agent

# 시연
![alt text](data/0703_unity_mcp1.gif)


로컬 LLM(Ollama)으로 Unity Editor를 제어하는 채팅 CLI. 현재 버전은 **v1.9.1**이다.
기존 [unity_mcp](../unity_mcp) MCP 서버와 Unity 브리지를 재사용하고
(v1.7부터 send_key 등 일부 확장), Claude Code의 자리를 로컬 모델이 대신한다.

```
[사용자] ⇄ CLI 채팅 (main.py)
              │
        플래너 (planner.py, 큰 요청 → 마일스톤 분해)          ← v1.7
              │
        제작 Agent 루프 (agent.py) ⇄ Ollama (qwen3-coder:30b, localhost:11434)
              │  tool_calls  ↕ TaskContract 정책 게이트 (task_contract.py)
        MCP 클라이언트 (mcp_client.py)
              │  stdio (uv run server.py 자식 프로세스)
        unity_mcp/server.py
              │  TCP 127.0.0.1:8722 JSON-lines
        Unity Editor (UnityMcpBridge.cs)
              ↑
        v1.9 호스트 독립 검증 → 실패 항목 수정/재검증 → receipts JSON
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
연결됨: 28 tools · qwen3-coder:30b · ctx 32768
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
| `/tools` | 사용 가능한 도구 목록 (MCP 23개 + 로컬 7개) |
| `/model <이름>` | 모델 변경 (예: `/model qwen3:32b`) |
| `/last` | 마지막 도구 결과 원본(절단 전) 보기 |
| `/log` | 마지막 요청의 텍스트/JSONL 실행 로그 경로 표시 (v1.8) |
| `/receipt` | 마지막 호스트 독립 검증 영수증 JSON 경로 표시 (v1.9) |
| `/verify <요청>` | 모델 자기판정 없이 호스트가 상태/플레이/입력 증거를 직접 검증 (v1.9) |
| `/look [질문]` | 마지막 스크린샷을 비전 모델로 분석 (`--vision` 필요) |
| `/quit` | 종료 |

### 로컬 도구 (호스트가 직접 실행)

MCP 서버의 21개 도구(`unity_send_key` 포함) 외에, 호스트가 직접 실행하는 도구 7개가 추가됩니다:

| 도구 | 동작 |
|---|---|
| `unity_write_script` | Unity 프로젝트 `Assets/` 아래에 C# 파일 생성/덮어쓰기 (경로 가드: `.cs`만, `..` 금지) |
| `unity_read_script` | 기존 C# 스크립트 읽기 |
| `unity_delete_script` | 기존 C# 스크립트 및 동반 `.cs.meta` 파일 삭제 (경로 가드: `Assets/Editor/McpBridge/` 하위 삭제 금지, `.cs`만 허용, `..` 금지) |
| `unity_wait` | 플레이 진입 후 런타임 에러 대기 (0.5~10초) |
| `unity_install_level_loader` | canonical `LevelLoader.cs` 템플릿을 `Assets/Scripts/`에 설치 (v1.7) |
| `unity_write_level` | 레벨 JSON을 `Assets/StreamingAssets/Levels/`에 작성 — 호스트가 스키마를 결정적으로 검증 (v1.7) |
| `unity_read_level` | 기존 레벨 JSON 읽기 (v1.7) |

에이전트의 스크립트 워크플로: `unity_write_script` → `unity_refresh_assets`(호스트가
컴파일 완료까지 자동 대기) → `unity_read_console`로 에러 확인 → `unity_add_component`.

레벨 워크플로(v1.7): `unity_install_level_loader` → 컴파일 확인 → `LevelLoader` 컴포넌트 부착 →
`unity_write_level`(재컴파일 불필요) → 플레이 모드에서 콘솔의 `[LevelLoader] Loaded` 마커로 검증.
게임플레이 검증: `unity_send_key`(tap/press/release)로 가상 키 입력을 주입하고 Player 위치 전후 비교.

### 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `UNITY_AGENT_MODEL` | `qwen3-coder:30b` | 사용할 Ollama 모델 |
| `UNITY_AGENT_NUM_CTX` | `32768` | 컨텍스트 길이. VRAM 부족(CPU 분할) 시 16384로 |
| `UNITY_AGENT_MAX_ITERS` | `15` | 한 턴의 최대 도구 호출 반복 |
| `UNITY_AGENT_STREAM` | `1` | 스트리밍 중 tool_calls가 안 오면 `0` |
| `UNITY_MCP_DIR` | `..\unity_mcp` 절대경로 | MCP 서버 위치 |
| `UNITY_PROJECT_DIR` | Unity 프로젝트 절대경로 | 스크립트 쓰기·브리지 포트 파일 위치 |
| `UNITY_AGENT_AUTO_OPEN` | `1` | 스크린샷 자동 열기 |
| `UNITY_AGENT_FOCUS_COMPILE` | `1` | 컴파일 대기 시 Unity 창에 잠깐 포커스 (백그라운드 컴파일 지연 방지) |
| `UNITY_AGENT_AUTO_CONSENT` | `1` | "Script Updating Consent" 모달 자동 수락 (구식 API 자동 변환 동의) |
| `UNITY_AGENT_PLANNER` | `auto` | 큰 요청 마일스톤 분해: `auto`(휴리스틱) / `always` / `off` (v1.7) |
| `UNITY_AGENT_MILESTONE_ITERS` | `12` | 마일스톤당 도구 호출 예산 (v1.7) |
| `UNITY_AGENT_MILESTONE_RETRIES` | `1` | 마일스톤 실패 시 재시도 횟수 (v1.7) |
| `UNITY_AGENT_FOCUS_INPUT` | `1` | `unity_send_key` 직전 Unity 창 포커스 — Unity가 백그라운드/최소화면 플레이어 루프가 멈춰 입력이 게임에 닿지 않음 (v1.7) |
| `UNITY_AGENT_RUN_LOGS` | `1` | 모든 `Agent.run_turn()`의 `.log` + `.jsonl` 자동 저장 (v1.8) |
| `UNITY_AGENT_RUN_LOG_DIR` | `logs/runs` | 실행 로그 루트 경로. 날짜별 하위 폴더가 자동 생성됨 (v1.8) |
| `UNITY_MCP_AUDIT_LOGS` | `1` | `UnityTools.call()` 전체 호출 감사 JSONL 저장 (v1.8.1) |
| `UNITY_MCP_AUDIT_LOG_DIR` | `logs/mcp` | MCP 감사 로그 루트 경로 (v1.8.1) |
| `UNITY_AGENT_TOOL_MODE` | `full` | `full` 또는 세션 전체 `verify` 도구 제한 (v1.8.1) |
| `UNITY_AGENT_VERIFY` | `1` | 제작 요청의 독립 검증·자동 수정·재검증 사용 (v1.9) |
| `UNITY_AGENT_FIX_CYCLES` | `3` | 독립 검증 실패 후 fresh 수정 컨텍스트 최대 횟수 (v1.9) |
| `UNITY_AGENT_FIX_ITERS` | `20` | 자동 수정 한 사이클의 모델 도구 반복 예산 (v1.9) |
| `UNITY_AGENT_TASK_TIMEOUT` | `1800` | 검증·자동 수정 전체 시간 예산(초). 무한 반복 대신 사용 (v1.9) |
| `UNITY_AGENT_NO_PROGRESS_LIMIT` | `2` | 같은 실패 집합이 반복될 때 조기 중단하는 횟수 (v1.9) |
| `UNITY_AGENT_RECEIPT_DIR` | `logs/receipts` | 검증 영수증 JSON 루트 경로 (v1.9) |

### 실행 로그 (v1.8)

CLI 요청, 플래너 오케스트레이션과 `e2e_v17_run.py` 같은 프로그램 호출은 모두
공통 `Agent.run_turn()` 경로에서 자동 기록됩니다.

```text
logs/runs/2026/07/21/
  20260721_120102_123_레벨_3개짜리_플랫포머_ab12cd34ef.log
  20260721_120102_123_레벨_3개짜리_플랫포머_ab12cd34ef.jsonl
```

- `.log`: 사람이 읽기 쉬운 전체 실행 기록
- `.jsonl`: 요청, 실행 모드, 마일스톤, 모델 응답, tool 인자/결과, 검증 누락,
  경고, 예외와 최종 상태를 이벤트 단위로 저장
- 정상 완료뿐 아니라 `failed`, `error`, `interrupted` 종료도 기록
- 로그에는 사용자 요청과 생성한 스크립트 원문이 포함될 수 있으므로 `logs/`는
  Git에서 제외됩니다.

마지막 실행 경로는 요청 종료 시 자동 표시되며 `/log`로 다시 볼 수 있습니다.
끄려면 `UNITY_AGENT_RUN_LOGS=0`을 설정합니다. 상세:
[docs/v1.8_run_logging.md](docs/v1.8_run_logging.md)

v1.8.1부터 `Agent.run_turn()`을 거치지 않는 직접 MCP 호출도
`logs/mcp/YYYY/MM/DD/*.jsonl`에 기록됩니다. `/verify <요청>`은 편집 도구를
스키마와 실행 경계 양쪽에서 차단합니다. 상세:
[docs/v1.8.1_reliability.md](docs/v1.8.1_reliability.md)

v1.9부터 제작 모델의 자연어 "완료"는 사용자에게 최종 결과로 노출되지 않습니다.
호스트가 활성 씬/저장 상태, 컴파일·런타임 오류, 필수 컴포넌트, Player 이동·점프,
카메라 추종, 실행 중 스크린샷, 입력 해제와 Play 종료를 직접 측정합니다. 실패하면
실패 항목만 fresh 수정 컨텍스트에 전달하고 다시 처음부터 측정합니다. 최종 판정과
측정값은 `logs/receipts/YYYY/MM/DD/*.json`에 저장됩니다. 상세:
[docs/v1.9_verification_orchestration.md](docs/v1.9_verification_orchestration.md)

## 트러블슈팅

- **모델이 도구를 "잊거나" 시스템 프롬프트를 무시함** — 컨텍스트 오버플로.
  Ollama 서버 로그에 `truncating input prompt`가 있으면 확정. `num_ctx`를 늘리거나 `/reset`.
- **응답이 갑자기 10배 느려짐** — 모델이 CPU로 분할됨. `ollama ps`가 `100% GPU`인지 확인.
  아니면 `UNITY_AGENT_NUM_CTX=16384`로 낮추기.
- **`<function=...>` 같은 텍스트가 답변에 섞임** — Ollama가 tool-call을 파싱하지 못함.
  Ollama를 최신 버전으로 업데이트.
- **Unity bridge unreachable** — Unity Editor가 열려 있고 콘솔에 `[McpBridge] Listening`이
  있는지 확인. 플레이 모드 전환/스크립트 컴파일 직후에는 몇 초 기다렸다 재시도.
- **브리지가 계속 무응답 (타임아웃)** — Unity에 **모달 대화상자**가 떠 있으면 에디터 메인
  스레드가 멈춰 브리지도 응답하지 못한다. 대화상자를 닫으면 즉시 복구.
- **"Script Updating Consent" 모달로 멈춤** — 모델이 구식 API(`rb.velocity` 등)를 쓰면
  Unity가 API 자동 변환 동의 모달을 띄워 브리지가 멈춘다. `unity_write_script`가 흔한 패턴을
  미리 교정하고, 그래도 뜨면 호스트가 자동으로 "전체 동의" 버튼을 클릭한다.
  끄려면 `UNITY_AGENT_AUTO_CONSENT=0`. 상세: [docs/v1.2_consent_modal_fix.md](docs/v1.2_consent_modal_fix.md)
- **"The open scene(s) have been modified externally" 프롬프트** — 2단계로 자동 처리된다.
  깨끗한(clean) 씬은 `SceneAutoReload.cs`(에디터 스크립트)가 프롬프트 없이 자동 리로드하고,
  **저장 안 한 변경이 있는 씬(dirty)**은 프롬프트가 뜨되 호스트가 **Ignore**(현재 상태 유지,
  데이터 손실 없음)를 자동 클릭해 브리지 마비를 막는다. 에디터 쪽은 Tools ▸ MCP Bridge ▸
  Auto-Reload Scenes On External Change로, 호스트 쪽은 `UNITY_AGENT_AUTO_CONSENT=0`으로 끔.
- **브리지 포트가 8722가 아님** — 도메인 리로드로 고아가 된 소켓이 포트를 점유하면
  브리지가 다음 포트(8723, 8724…)로 옮겨 바인드하고 실제 포트를
  `<Unity 프로젝트>/Library/McpBridgePort.txt`에 기록한다. 에이전트는 이 파일을 자동으로
  읽고, 세션 중 포트가 바뀌면 MCP 서버를 재기동해 따라간다. Unity를 재시작하면 8722로 복귀.
- **첫 응답이 20~40초 느림** — 모델 로딩. 이후엔 `keep_alive=30m`으로 메모리에 유지됨.
- **한글 깨짐** — CLI는 UTF-8 replacement 모드로 동작해 출력 인코딩 오류가 Unity 작업을
  중단하지 않습니다. 셸 자체 표시는 Windows Terminal 사용을 권장합니다.
- **비정상 종료 후 `uv`/`python` 프로세스가 남음** — 작업 관리자에서 정리.
- **반복적인 오브젝트 생성으로 인한 턴 중단 (MAX_ITERS 초과)** — 3개 이상의 유사 오브젝트 생성 시 반복 호출 대신 배치 생성 툴(`unity_create_gameobjects`)을 활용하도록 프롬프트 수준에서 제약하며, 20개 이상의 대형 그리드/레이아웃은 툴 호출 대신 MonoBehaviour 스크립트의 `Awake()`/`Start()` 단에서 동적 생성하도록 유도합니다. 또한 같은 툴의 다회 반복을 감지하는 루프 가드(기본 4회)가 백그라운드에서 경고 넛지를 제공합니다. 상세: [docs/v1.3_batch_creation.md](docs/v1.3_batch_creation.md)
- **컴파일이 완전히 깨져 스크립트 수정이 불가능함 (중복 클래스 에러 등)** — 이전 세션의 잔재 등으로 컴파일 에러 상태가 지속되면 에디터 브리지도 재컴파일이 불가해 멈추게 됩니다. 이때는 호스트가 직접 디스크에서 스크립트 및 동반 메타 파일을 지우는 `unity_delete_script`를 활용하여 오류 스크립트를 제거하고 `unity_refresh_assets`를 통해 강제 리프레시할 수 있습니다. 상세: [docs/v1.4_delete_script.md](docs/v1.4_delete_script.md)
- **Ollama의 도구 호출 누수 및 JSON 형식 에러** — 모델이 C# 스타일 float 접미사(`0.9f` 등)를 JSON 인자로 잘못 전달할 경우 호스트에서 파싱 전 자동 보정하며, 정상 호출과 텍스트로 누수된 호출(`<function=...>` 포맷)이 혼합되어 수신되는 현상을 `_merge_leaked_calls`가 자동으로 병합하여 유실 없이 복구합니다. 상세: [docs/v1.3_batch_creation.md](docs/v1.3_batch_creation.md), [docs/v1.4_delete_script.md](docs/v1.4_delete_script.md)

## 파일 구성

| 파일 | 역할 |
|---|---|
| `config.py` | 모델·컨텍스트·한도 등 설정 |
| `mcp_client.py` | MCP stdio 세션, 도구 스키마 → Ollama 형식 변환, 인자 보정(C# float 자동 제거 포함)·결과 절단, 컴파일 대기, 포트 추적 재접속 |
| `local_tools.py` | 호스트 로컬 도구 (스크립트 read/write/delete, 레벨 read/write, 로더 설치), 구식 API 자동 교정 |
| `agent.py` | 시스템 프롬프트 + tool-call 루프(`_react_loop`), 플래너 디스패치·마일스톤 실행, 누수 및 혼합 응답 tool-call 복구 파서 |
| `planner.py` | 큰 요청 판별(`looks_large`), 계획 JSON 생성·검증, 산출물 대장(ArtifactLedger) (v1.7) |
| `task_contract.py` | 실행 전 정책 게이트 + 완료 전 검증 마일스톤 강제 (v1.5~) |
| `run_logging.py` | 요청별 `.log`/`.jsonl` 트랜스크립트 및 종료 상태 기록 (v1.8) |
| `audit_logging.py` | Agent 밖의 직접 호출까지 포함하는 MCP 세션 감사 JSONL (v1.8.1) |
| `verification.py` | 요청별 검증 명세, Unity 실측 증거, 호스트 판정과 영수증 (v1.9) |
| `level_schema.py` | 레벨 JSON 스키마 + 결정적 검증 (v1.7) |
| `templates/LevelLoader.cs` | canonical 데이터 주도 레벨 로더 템플릿 (v1.7) |
| `winfocus.py` | 컴파일 시 Unity 창 포커스 유틸 |
| `winmodal.py` | "Script Updating Consent" 모달 자동 클릭 유틸 |
| `main.py` | REPL, 슬래시 명령, 스크린샷 처리 |
| `mcp_server.log` | MCP 서버 stderr 진단 로그. 사용자 요청 실행 기록은 아님 |
| `logs/runs/YYYY/MM/DD/` | 요청별 실행 트랜스크립트 (`.log` + `.jsonl`, Git 제외) |
| `logs/mcp/YYYY/MM/DD/` | 모든 `UnityTools.call()` 감사 로그 (`.jsonl`, Git 제외) |
| `logs/receipts/YYYY/MM/DD/` | 요청별 독립 검증 결과와 측정값 영수증 (`.json`, Git 제외) |


## 버전별 개선 사항
- ver 1.0 - 프로토타입 MVP

- ver 1.1 - C# 스크립트 작성 도구 (unity_write_script/unity_read_script), 컴파일 자동 대기,
          브리지 포트 호핑 대응 (UnityMcpBridge.cs 안정화 수정 포함)

- ver 1.2 - "Script Updating Consent" 모달 자동 처리 (구식 API 교정 + Win32 자동 클릭),
          시스템 프롬프트에 Unity 6 API/Input System 규칙 추가

- ver 1.3 - 반복 오브젝트 생성으로 인한 턴 중단 해결 (`unity_create_gameobjects` 배치 툴 추가, 시스템 프롬프트 규칙 유도, C# float 인자 보정, 반복 호출 루프 가드)

- ver 1.4 - 스크립트 파일 직접 삭제  (`unity_delete_script` 로컬 툴 및 브리지 소스 보호),  혼합 응답(normal + text) tool-call 복구 파서 개선

- ver 1.4.1 - `read_console` stale 에러 수정 (브리지가 컴파일 시작 시 logBuffer를 비움)

- ver 1.5/1.6 - 에이전트 실행 정책 + TaskContract (실행 전 정책 게이트, 완료 전 검증 마일스톤 강제)

- ver 1.7 - 호스트 측 플래너 (큰 요청 → 마일스톤 분해, 마일스톤별 fresh 컨텍스트 + 산출물 대장),
          데이터 주도 레벨 시스템 (`unity_write_level` 스키마 검증 + canonical `LevelLoader.cs`),
          키 입력 시뮬레이션 (`unity_send_key` — 브리지 Input System 가상 키보드 주입).
          상세: [docs/v1.7_planner_levels_input.md](docs/v1.7_planner_levels_input.md)

- ver 1.8 - 모든 `Agent.run_turn()` 실행에 요청별 이중 로그(`.log` + `.jsonl`) 자동 저장.
          단일/플래너 실행, 마일스톤, 모델 응답, tool 원문, 검증 누락, 경고와
          정상/실패/예외/중단 종료 상태를 기록. CLI `/log` 명령 추가.
          상세: [docs/v1.8_run_logging.md](docs/v1.8_run_logging.md)

- ver 1.8.1 - Agent 밖의 직접 MCP 호출 감사 로그, `/verify` 검증 전용 모드, Unicode 출력
          장애 격리, 재시도 시 기존 산출물 재사용 지침 추가. Unity 브리지 0.2.1에서
          컴포넌트 멱등 추가/제거, 플레이 모드 편집 원자적 차단, 스크린샷 폴더 자동 생성,
          입력 상태 조회를 지원.
          상세: [docs/v1.8.1_reliability.md](docs/v1.8.1_reliability.md)

- ver 1.9 - 제작 모델에서 완료 판정권을 회수. 호스트가 Unity를 직접 조작해 씬 저장,
          컴파일/런타임 오류, 필수 컴포넌트, 이동·점프·카메라 추종 좌표 변화,
          플레이 중 스크린샷, 입력 해제와 최종 Play 종료를 독립 검증합니다.
          실패 항목만 fresh 컨텍스트에서 자동 수정한 뒤 재검증하며, 시간 예산과
          무진전 감지로 종료합니다. 결과는 별도 검증 영수증 JSON으로 보존합니다.
          상세: [docs/v1.9_verification_orchestration.md](docs/v1.9_verification_orchestration.md)

- ver 1.9.1 - 로그 분석에서 확인된 검증 공백을 보완. 요청에 명시된 스크립트
          클래스명을 실제 컴포넌트 검증에 사용하고, Play 진입 응답 뒤 실제
          `isPlaying` 상태를 재확인합니다. A/D 양방향 이동과 Shift 부스트 거리
          비교를 지원하며, 점프는 복수 샘플로 최고점을 측정합니다. 단일 씬 MVP가
          명시되지 않은 레벨 로더/다단계 계획을 자동 제거하고, 예기치 않은 Play
          종료를 별도 실패로 기록합니다.
