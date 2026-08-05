# Unity Local Agent

# 시연

### 🎬 v1.11.11 시연 영상
[![v1.11.11 시연 영상](https://img.youtube.com/vi/HOmAOHZUyfQ/hqdefault.jpg)](https://www.youtube.com/watch?v=HOmAOHZUyfQ)

> 🔗 [YouTube에서 영상 보기](https://www.youtube.com/watch?v=HOmAOHZUyfQ)

![alt text](data/0703_unity_mcp1.gif)


로컬 LLM(Ollama)으로 Unity Editor를 제어하는 채팅 CLI. 현재 버전은 **v1.11.26**이다.
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
- 옆 폴더의 `unity_mcp` 프로젝트. Unity 브리지와 필수 패키지는 선택한 Unity 프로젝트에
  자동 설치된다. 연결 전 Unity Console에 `[McpBridge] Listening`이 보여야 한다.

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
uv run main.py --project "D:\UnityProjects\MyGame"  # 프로젝트 지정
uv run main.py --project "D:\UnityProjects\MyGame" --vision  # 프로젝트 지정 + 비전
uv run main.py --project "D:\UnityProjects\MyGame" --prompt-file docs\request.txt
uv run main.py --project "D:\UnityProjects\MyGame" --prompt-file docs\request.txt --repair-existing
```

### 새 Unity 프로젝트 빠른 시작

1. Unity Hub에서 새 프로젝트를 만들고 열어 둡니다.
2. `uv run main.py`를 실행합니다.
3. 최근 프로젝트를 쓰려면 Enter, 다른 프로젝트를 고르려면 경로를 붙여넣거나 `b`를 입력합니다.
4. 브리지와 패키지가 처음 설치되는 경우 Unity가 컴파일을 마칠 때까지 기다립니다.

시작 상태는 `[1/4] 프로젝트 선택`, `[2/4] 브리지·패키지 준비`, `[3/4] Unity 연결 확인`,
`[4/4] 연결 완료` 순서로 표시됩니다. 컴파일·도메인 리로드로 연결이 지연될 경우에도
현재 대기 이유를 알 수 있습니다.

선택한 프로젝트에 `UnityMcpBridge.cs`가 없으면
`Assets/Editor/UnityMcpBridge.cs`로 자동 설치합니다. 또한 필요한 `Input System`과
`Newtonsoft Json`을 Package Manager manifest에 추가하고 Input Handling을 `Both`로 설정합니다.
기존 브리지와 이미 지정된 패키지 버전은 덮어쓰지 않습니다. 설치 직후 Unity Console에
`[McpBridge] Listening`이 표시되면 CLI가 연결을 재시도합니다.

`--prompt-file`은 UTF-8 요청 전체를 한 번 실행하고 성공/실패 종료 코드를 반환합니다.
`--repair-existing`을 함께 쓰면 초기 builder를 생략하고 현재 산출물을 먼저 측정한 뒤
실패 조건만 수정하므로, 반복 검증에서 이미 성공한 씬을 재생성하는 오염을 줄입니다.

### Unity 프로젝트 전환

프로젝트를 바꿀 때는 처음 한 번만 Unity 프로젝트 루트를 `--project`로 지정합니다.
선택한 절대경로는 Git에서 제외되는 `.unity-local-agent.json`에 저장되므로, 다음부터는
`uv run main.py`만 실행해도 최근 프로젝트에 다시 연결됩니다.

프로젝트 선택 우선순위는 다음과 같습니다.

1. `--project PATH` 실행 인자
2. `UNITY_PROJECT_DIR` 환경변수
3. `.unity-local-agent.json`에 저장된 최근 프로젝트
4. `config.py`의 기존 기본값

명시적으로 지정한 폴더에 `Assets/`와 `ProjectSettings/`가 없으면 MCP 서버를 시작하기
전에 읽기 쉬운 오류와 종료 코드 `2`를 반환합니다. 여러 Unity Editor를 동시에 열어도
선택한 프로젝트의 `Library/McpBridgePort.txt`를 읽으므로 해당 프로젝트 브릿지가 실제로
사용 중인 포트(8722, 8723 등)를 따라갑니다.

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
| `UNITY_AGENT_MAX_ITERS` | `30` | 한 턴의 최대 도구 호출 반복 |
| `UNITY_AGENT_STREAM` | `1` | 스트리밍 중 tool_calls가 안 오면 `0` |
| `UNITY_AGENT_MODEL_RETRIES` | `1` | EOF·연결 끊김·일시적 서버 오류가 난 모델 호출의 재시도 횟수 |
| `UNITY_MCP_DIR` | `..\unity_mcp` 절대경로 | MCP 서버 위치 |
| `UNITY_PROJECT_DIR` | Unity 프로젝트 절대경로 | 스크립트 쓰기·브리지 포트 파일 위치. `--project`가 있으면 실행 인자가 우선 |
| `UNITY_AGENT_AUTO_OPEN` | `1` | 스크린샷 자동 열기 |
| `UNITY_AGENT_FOCUS_COMPILE` | `1` | 컴파일 대기 시 Unity 창에 잠깐 포커스 (백그라운드 컴파일 지연 방지) |
| `UNITY_AGENT_AUTO_CONSENT` | `1` | "Script Updating Consent" 모달 자동 수락 (구식 API 자동 변환 동의) |
| `UNITY_AGENT_PLANNER` | `auto` | 큰 요청 마일스톤 분해: `auto`(휴리스틱) / `always` / `off` |
| `UNITY_AGENT_SCENE_PATH_POLICY` | `strict` | 충돌하는 씬 경로 처리: `strict`는 변경 전 중단, `acceptance`는 합격 조건의 단일 경로로 정규화 |
| `UNITY_AGENT_ALLOW_UNSCOPED_SCRIPT_READ` | `0` | `1`이면 `Assets/Scripts/*.cs` 기존 파일을 경로 명시 없이 읽을 수 있음. 삭제 권한은 계속 요청 범위로 제한 |
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
| `UNITY_AGENT_REPAIR_ROLLBACK` | `1` | 자동 수정이 상태를 악화시키면 최선 사이클의 파일로 되돌림 (v1.11.5) |

### 실행 로그 (v1.8)

CLI 요청, 플래너 오케스트레이션과 프로그램 호출은 모두
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
| `project_settings.py` | CLI·환경변수·최근 기록을 이용한 Unity 프로젝트 선택 및 경로 검증 |
| `docs/STATUS.md` | **지금 열려 있는 결함·과제의 단일 출처.** 갱신하며 쓴다 |
| `docs/vX.Y.Z_*.md` | 버전별 불변 증거 기록 (배경·변경·검증). 현재 상태가 아님 |
| `mcp_server.log` | MCP 서버 stderr 진단 로그. 사용자 요청 실행 기록은 아님 |
| `logs/runs/YYYY/MM/DD/` | 요청별 실행 트랜스크립트 (`.log` + `.jsonl`, Git 제외) |
| `logs/mcp/YYYY/MM/DD/` | 모든 `UnityTools.call()` 감사 로그 (`.jsonl`, Git 제외) |
| `logs/receipts/YYYY/MM/DD/` | 요청별 독립 검증 결과와 측정값 영수증 (`.json`, Git 제외) |


## 버전별 개선 사항

> **지금 무엇이 열려 있는지**는 [docs/STATUS.md](docs/STATUS.md)에서 읽는다.
> 아래 히스토리와 `docs/vX.Y.Z_*.md`는 그때 무엇을 왜 고쳤고 무엇을 실측했는지의
> 기록이지, 현재 상태가 아니다.

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
          `isPlaying` 상태를 재확인합니다.(예:A/D 양방향 이동과 Shift 부스트 거리
          비교를 지원하며, 점프는 복수 샘플로 최고점을 측정합니다.) 단일 씬 MVP가
          명시되지 않은 레벨 로더/다단계 계획을 자동 제거하고, 예기치 않은 Play
          종료를 별도 실패로 기록합니다.

- ver 1.9.2 - Unity 브릿지 연결 편의성을 개선. `--project PATH`로 대상 Unity
          프로젝트를 즉시 선택하고, 유효한 프로젝트 경로를 로컬 설정에 기억해 다음
          실행에서 자동 재사용합니다. CLI·환경변수·최근 프로젝트·기본값 우선순위를
          명확히 하고, 잘못된 프로젝트는 브릿지 연결 전에 검증해 안내합니다.

- ver 1.10.0 - 요청 preflight, 합격 조건 기반 단일 씬 경로 정규화, 프로젝트 identity
          검증과 브릿지 재연결 상태 기록을 추가했습니다. verifier가 Collider,
          Rigidbody 제약 비트, 카메라 target/Z, 정확한 입력 지속시간, 착지, 좌우
          부스트와 입력 해제를 직접 측정합니다. `--repair-existing`, 정적 정책 lint,
          결정적 안전 repair, 강제 저장 장벽, 신규 행동 회귀/무진전 중단으로 사람의
          복구 개입과 성공 산출물 재생성을 줄였습니다.

- ver 1.11.0 - 일반 실행에서 Unity 프로젝트를 대화형으로 선택할 수 있게 했습니다.
          선택한 프로젝트에 브리지가 없으면 `UnityMcpBridge.cs`를 자동 설치하고,
          `Input System`과 `Newtonsoft Json` 패키지 및 Input Handling(Both)을 안전하게
          보완합니다. 신규 빈 Unity 프로젝트에서 패키지 설치, 브리지 컴파일,
          ### 1.11 의 경우 테스트 를 하다보면, 컨텍스트 오염을 방지하기위해서, 새로운 프로젝트를 생성할때마다, 브릿지를 연결하는 작업이 길어져서 시작부분 자동화를 추가하였음.

- ver 1.11.1 - 시작 과정을 4단계 상태 표시로 개선했습니다. 실제 `unity_ping`이 성공한
          뒤에만 연결 완료를 출력하므로, Unity 컴파일·도메인 리로드 대기와 연결 실패를
          즉시 구분할 수 있습니다.

- ver 1.11.2 - **검증 공집합 성공 결함(P0) 수정.** "Space 점프가 동작하지 않는다.
          수정하고 검증한다" 같은 요청이 `게임` 키워드가 없다는 이유로 행동 검증을
          전부 끄고, Play Mode를 한 번도 돌리지 않은 채 `verified`로 기록되던 문제를
          고쳤습니다. 점프·이동·착지·부스트·카메라를 게임 키워드와 독립적으로 감지하고,
          행동 조건이 하나라도 잡히면 Play Mode를 강제합니다. 측정 가능한 조건을
          못 뽑은 동작 요청은 성공 대신 `verification_spec_empty`로 종료하며,
          영수증에 `requested_checks`/`measured_checks`/`skipped_checks`를 명시합니다.
          영수증 전수 조사 결과 기존 `verified` 3건이 전부 Play Mode 미실행 상태였습니다.
          상세: [변경 내용](docs/v1.11.2_verification_spec_reliability.md) ·
          [진행 현황 보고서](docs/v1.11.2_status_and_improvement_report.md)

- ver 1.11.3 - **repair 회귀 오판 수정(P1).** 실측 E2E에서 자동 수정이 3회 예산 중
          1회 만에 중단되던 문제를 고쳤습니다. 컴파일·컴포넌트가 고쳐져 게임플레이가
          **처음으로 측정 가능해진 것**을 "새로 생긴 실패"로 보고 회귀 판정하던 것이
          원인입니다. 이제 이전 사이클의 `measured_checks`를 기준으로 최초 측정과
          진짜 회귀를 구분하고, `compile_errors:3 → 1` 같은 개수 감소도 진전으로
          인정합니다. 재실행 결과 repair가 3회 예산을 완주하며 점프 버그를 실제로
          수정했고, 통과하던 검사가 깨지자 정확히 회귀로 중단했습니다.
          상세: [docs/v1.11.3_repair_regression_detection.md](docs/v1.11.3_repair_regression_detection.md)

- ver 1.11.4 - **이동 검증 공백 수정 — 프로젝트 최초의 정당한 `verified` 달성.**
          두 결함을 고쳤습니다. ① 이동 검사에 하한만 있어 매 FixedUpdate의 누적
          `AddForce(Impulse)`로 1초에 131유닛을 이동하는 폭주 물리가 통과하던 문제 →
          속도 기반 상한(`movement_max_speed=25`) 도입. ② 요청은 "A/D 이동"인데
          검증은 `rightArrow`를 하드코딩해 정상 동작하는 게임을 실패시키던 문제 →
          요청에서 제어 방식을 추출해 해당 키로 검증하고, A/D·방향키 표본을 서로
          인정합니다. 재실행 결과 1차에서 폭주를 잡고 repair가 이를 고쳐
          **`measured_checks`가 모두 채워진 `verified`**를 처음으로 기록했습니다
          (D 이동 126→5.0, 점프 최고점 1.1→6.07).
          상세: [docs/v1.11.4_movement_verification_gaps.md](docs/v1.11.4_movement_verification_gaps.md)

- ver 1.11.5 - **repair snapshot/rollback(P1).** 자동 수정이 한 버그를 고치면서 다른
          버그를 만들면 더 나쁜 마지막 상태가 프로젝트에 남던 문제를 해결했습니다.
          매 검증 직후 상태를 점수화해 최선 시점의 파일(`Assets/Scripts`, 레벨 JSON,
          씬)을 스냅샷하고, 루프가 최선보다 나쁘게 끝나면 되돌린 뒤 **재검증해서**
          영수증이 실제 상태를 기술하도록 합니다. 구현 중 실측에서 `no_verification_progress`
          같은 루프 마커를 결함으로 세어 모델의 올바른 수정을 되돌리는 거짓 롤백을
          발견해 함께 고쳤습니다. `UNITY_AGENT_REPAIR_ROLLBACK=0`으로 끌 수 있습니다.
          상세: [docs/v1.11.5_repair_rollback.md](docs/v1.11.5_repair_rollback.md)

- ver 1.11.6 - **점프 키 명세 + 개발 의존성 정리.** v1.11.4가 이동 키에 대해 고친 것과
          같은 종류의 거짓 실패가 점프에도 있었습니다 — 요청이 "W키로 점프"여도 검증은
          `space`를 하드코딩해 정상 게임을 실패시켰습니다. 요청에서 점프 키를 추출해
          해당 키로 검증합니다. 또한 `pytest`/`pytest-cov`를 dev 의존성 그룹으로 선언해
          `uv run python -m pytest tests/`가 별도 `--with` 없이 동작합니다.
          상세: [docs/v1.11.6_jump_key_and_dev_deps.md](docs/v1.11.6_jump_key_and_dev_deps.md)

- ver 1.11.7 - **엄격한 새 씬 전체 E2E 첫 통과 + 검증 배관 신뢰성 보강.** 실행 전에
          씬과 `.meta`가 모두 없음을 확인한 경로를 `--repair-existing` 없이 로컬 모델에
          맡겨, 단일 실행 안에서 제작·repair·독립 검증까지 처음으로 통과했습니다.
          미측정 상태를 우대하던 rollback 점수, 도메인 리로드 중 Play Mode 오판,
          리로드로 사라진 컴파일 오류, rollback 뒤 메모리 씬 잔존, 미요청 LevelLoader,
          짧은 점프 입력 누락, 일시적 Ollama EOF를 고쳤습니다. 최종 영수증은 요청한
          5개 검사를 모두 측정했고 `skipped_checks=[]`를 기록했습니다.
          상세: [docs/v1.11.7_fresh_scene_e2e_reliability.md](docs/v1.11.7_fresh_scene_e2e_reliability.md)

- ver 1.11.8 - **빌더 자체 완결성 확보.** 빌더와 첫 호스트 검증 사이에 정규 씬 저장
          장벽을 추가하고, 명시적인 A/D·Space 요청은 직접 `Keyboard.current` 구현으로
          수렴시켰습니다. 짧은 점프 입력과 정의되지 않은 Ground 태그를 결정적으로
          정규화하고, 브리지는 누른 키 상태를 매 Editor tick 다시 큐잉합니다. 실행 전
          존재하지 않던 새 씬의 전체 E2E가 repair 없이 첫 검증에서 5/5를 측정해
          `build_stage_success=true`, `attempts=1`, `skipped_checks=[]`를 기록했습니다.
          상세: [docs/v1.11.8_builder_stage_completeness.md](docs/v1.11.8_builder_stage_completeness.md)

- ver 1.11.9 - **점프 상한 검증과 하네스 대필 제거.** v1.11.8은 20유닛 솟아올라
          착지하지 못한 점프를 `verified`로 통과시켰습니다. 이동에만 있던 sanity 상한을
          점프에도 추가하고(`player_jumped_too_high`), 임펄스가 중첩되지 않는 rising
          edge 래치를 표준으로 요구합니다. 정책 게이트가 모델의 스크립트를 정규식으로
          직접 고쳐 쓰던 동작은 제거해, 영수증이 다시 로컬 모델의 산출물을 가리키게
          했습니다. 새 씬 E2E에서 점프 상승량이 +19.96 → **+4.98**로 잡히고
          `jump_landed=true`, 요청 6개 검사를 모두 실측했습니다. 다만 게이트가 엄격해진
          대가로 빌더가 iteration 한도에 닿아 `build_stage_success`는 false로
          후퇴했습니다(repair 1회로 통과).
          상세: [docs/v1.11.9_bounded_jump_and_unrewritten_scripts.md](docs/v1.11.9_bounded_jump_and_unrewritten_scripts.md)

- ver 1.11.10 - **정책 게이트 오탐 제거.** v1.11.9의 게이트는 정상 동작하는 접지 코드를
          문자열 `.bounds`가 없다는 이유로 막아 빌더를 14회 차단시켰고, 모델이 파일명을
          바꾸며 스크립트를 271자까지 줄이는 회피에 예산을 소진했습니다. 접지 판정을
          철자에서 실제 결함(대입되지 않는 인스펙터 필드)으로 바꾸고, Update가 호출하는
          헬퍼까지 추적하며, 차단 메시지에 라벨 대신 실행 가능한 코드를 첨부합니다.
          바이트 동일 재작성도 차단합니다. 새 씬 E2E가 write 2회·repair 0회로
          `build_stage_success=true`, `attempts=1`, 6개 검사 전부 실측했습니다.
          상세: [docs/v1.11.10_gate_false_positive.md](docs/v1.11.10_gate_false_positive.md)

- ver 1.11.11 - **통과의 재현성 측정(n=9)과 빈 remedy 헤더 제거.** v1.11.10의
          `build_stage_success=true`는 n=1이었습니다. 씬 경로만 바꿔 새 씬 E2E를 9회
          돌린 결과 **9/9가 repair 0회로 첫 검증을 통과**했고, 점프 상승량이
          +4.96~+4.99로 상한 아래 안정적으로 모였습니다. 그중 3회는 남은
          `PlayerMovement.cs`를 매번 지우고 시작해, 이전 산출물을 베껴 통과한 것이
          아님을 확인했습니다(9회 모두 `unity_read_script` 호출 0회). 접지 오탐 재발은
          0회이고, 네 번의 차단은 모두 실제 결함으로 다음 write에서 곧바로 통과했습니다.
          측정 중 드러난 결함 하나 — 스니펫이 없는 항목에서 "이 형태로 고쳐라" 헤더
          아래가 비어 나가던 문제 — 를 고쳤고, 그 수정이 라이브 실행에서 그대로
          동작하는 것까지 확인했습니다.
          상세: [docs/v1.11.11_reproducibility_and_empty_remedy.md](docs/v1.11.11_reproducibility_and_empty_remedy.md)

- ver 1.11.12 - **다른 요청 형태에서 드러난 결함 셋.** v1.11.11의 9/9는 "A/D 이동 +
          Space 점프" 프롬프트 하나의 숫자였습니다. 카메라 추종 요청으로 같은 측정을
          하자 **빌더 자체 완결성이 0/3**으로 무너졌습니다. (1) 빈 씬 템플릿에는 카메라가
          없어 모든 검사가 측정 전에 차단되고, (2) 미정의 Ground 태그 게이트가 점프 요청
          안에만 걸려 있어 카메라 요청은 런타임 예외를 냈으며, (3) Play 종료 후 남은
          런타임 오류를 컴파일 오류로 계산해 실행 하나를 통째로 rollback시켰습니다.
          콘솔 분류를 실행 상태에서 진단 내용으로 바꾸고, 태그 검사를 씬 조건으로 옮기고,
          카메라가 필요한 요청의 빈 템플릿을 차단합니다. 재측정에서 카메라 추종이
          **3/3 `build_stage_success=true`**(이 형태로는 처음), 점프 형태는 회귀 없이
          11/11입니다.
          상세: [docs/v1.11.12_request_shape_generalization.md](docs/v1.11.12_request_shape_generalization.md)

- ver 1.11.13 - **공집합 성공의 재발과 프로젝트 가드 구멍.** 아직 측정된 적 없는 세
          형태(레벨 데이터·부스트·격자 절차 생성)를 측정했습니다. 레벨과 부스트는
          통과했지만, **격자 요청은 검사 항목이 0개로 추출되어 Play Mode에 들어가지도
          않고 40초 만에 `verified`가 나왔습니다** — v1.11.2가 잡은 공집합 성공이 그때의
          가드가 다루지 않는 형태로 되돌아온 것입니다. 가드가 "행동을 말한 요청"만
          지키고 "증명을 요구한 요청"은 지키지 않아서였고, 이제 만들라고 했고 검증을
          명시했는데 검사가 없으면 `verification_spec_empty`로 실패합니다. 또 Editor가
          다른 프로젝트에 열려 있을 때 브리지 호출은 거부되는데 **호스트 파일 도구는
          그대로 써져** 엉뚱한 프로젝트에 스크립트가 생기던 구멍도 막았습니다.
          점프 형태는 회귀 없이 통산 13/13입니다.
          상세: [docs/v1.11.13_empty_spec_and_project_guard.md](docs/v1.11.13_empty_spec_and_project_guard.md)

- ver 1.11.14 - **부분 집합을 전체로 판정하는 것을 보이게 했습니다.** v1.11.13은 검사가
          하나도 없을 때만 막았는데, 더 위험한 쪽은 "A/D 이동 + 코인에 닿으면 점수 +1"
          처럼 **이동은 측정되고 요청의 핵심은 아무 검사도 없는** 경우였습니다.
          `measured_checks`가 비어 있지 않아 기존 가드에 걸리지 않고 `verified`가
          나갔습니다. 이제 요청에서 어떤 검사로도 매핑되지 않는 절을
          `unmapped_requirements`로 영수증과 화면에 남깁니다. 저장소 프롬프트 32개에서
          오탐 0, 덮이지 않는 요청 5종 전부 감지했습니다. **판정은 바꾸지 않는 기록
          단계**입니다 — 게이트로 만들지는 오탐률을 더 본 뒤 정합니다. 빌더가 예산을
          어디에 쓰는지도 로그에 남기기 시작했고, 첫 데이터가 "절반을 자가 검증에
          쓴다"는 가설을 반박했습니다(요청에 따라 3% ~ 69%). 사용자가 준 실제 문장
          하나("3층짜리 플랫폼 … ad키로 좌우 … 좌쉬프트키로 가속 … 카메라가 추적")가
          **추출 공백 넷**을 더 드러냈습니다 — `ad키`(구분자 없는 표기)를 놓쳐 방향키로
          측정하려 했고, `좌쉬프트`·`추적`·`좌우`도 각각 부스트·카메라·양방향 검사를
          만들지 못했습니다. 어휘를 넓혀 검사가 4개 → 8개가 됐고, 라이브에서 부스트
          수정 안내 부재와 "층을 머리 위에 쌓아 점프가 물리적으로 불가능한 배치"까지
          잡아 세 번째 실행이 통과했습니다.
          상세: [docs/v1.11.14_unmapped_requirements.md](docs/v1.11.14_unmapped_requirements.md)

- ver 1.11.15 - **카메라가 관찰인지 시점인지, 부스트에 상한.** 사용자가 결과를 직접
          열어보고 "카메라가 관찰이 아니라 시점"이라고 알려줬는데, **영수증은 그 실행을
          통과로 기록하고 있었습니다.** 플레이어에 붙은 카메라는 변위가 정확히 같아
          추종 검사를 완벽하게 통과하고, 영수증에 델타만 남아 사후 구분도 불가능했습니다.
          카메라-플레이어 거리로 판정하고(`camera_is_player_viewpoint`) 그 거리를
          영수증에 남깁니다. 또 `CameraController.cs`가 파일명의 `controller` 때문에
          플레이어 입력 스크립트로 오인돼 **한 실행에서 13회 차단**되며 빌더 예산의
          절반을 태우고 있었습니다 — 이것만 고치자 복합 요청이 **3사이클 202초 →
          1사이클 82.8초, `build_stage_success=true`**로 처음 혼자 끝났습니다.
          부스트는 하한만 있어 0.5초에 140유닛(일반의 56배) 날아간 대시가 통과했고,
          기록된 28건의 분포(정상 1.0~6.6배, 고장 53~60배)를 근거로 10배 상한을
          넣었습니다.
          상세: [docs/v1.11.15_camera_observation_and_boost_cap.md](docs/v1.11.15_camera_observation_and_boost_cap.md)

- ver 1.11.16 - **만들어졌는지 보지 않고 통과하던 두 자리.** "댕댕이 모양의 모델링
          생성해줘"가 **검사 0개로 22초 만에 `verified`**가 났습니다. 런타임 어휘도
          검증 어휘도 없어 기존 두 가드를 모두 비껴갔고, 호스트는 컴파일 0건과 씬
          저장만 보고 통과시켰습니다 — 개가 만들어졌는지는 아무도 보지 않았습니다.
          이제 씬에 실체가 남아야 하는 생성 요청은 Edit Mode 계층을 읽어 기본
          오브젝트 외에 무엇이 생겼는지 재고 영수증에 이름으로 남깁니다
          (`scene_objects`). 같은 계열로, **렌더러 없는 Player가 검사 13개를 전부
          실측 통과한 적**이 있습니다(물리는 완벽했고 화면만 비어 있었습니다).
          `components["Player"]`가 Rigidbody와 Collider만 요구했기 때문인데, 이제
          Player 또는 그 자식에 Renderer가 있는지 봅니다(`player_visible`). 기록된
          계층 판독 73세션을 재생해 오탐 0건, 적발은 그 한 건뿐이었습니다. 라이브
          E2E 2회는 **repair 없이 1사이클 통과** — 모델링 요청은 37.3초에
          `measured_checks`가 `[]`에서 실측 1건(고양이 부위 8개)으로 바뀌었고,
          게임 요청은 77.2초에 검사 8개를 전부 실측했습니다.
          상세: [docs/v1.11.16_scene_content_and_player_visibility.md](docs/v1.11.16_scene_content_and_player_visibility.md)

- ver 1.11.17 - **빌더 예산: 무엇이 실제로 낭비인가.** 개선 리포트가 지목한 "빌더가
          매번 이터레이션 한도를 친다"의 전제부터 쟀더니, **54건 중 50건이 한도를
          치고 성공률은 54% 대 50%로 사실상 같았습니다** — 한도 소진은 거의 모든
          실행에 해당하는 상수이지 실패를 예측하는 변수가 아니었고, 코드 주석도
          그 값을 "diagnostic"이라 적어 두고 있었습니다. 실제 낭비는 **거부된
          호출**이었습니다: 66개 실행에서 190건, 그중 **63%가 단일 게이트**이고
          한 실행에서 **18회 연속** 걸려 예산의 60%를 태운 사례가 있었습니다.
          원인은 모델이 지적받은 항목을 고치며 스크립트를 통째로 다시 써 멀쩡한
          부분을 깨뜨리는 것이라, "다른 건 그대로 두라"는 지시를 2회차가 아니라
          **첫 차단부터** 주고, 4회 이상 수렴하지 않으면 게이트를 통과하는 **전체
          형태**를 줍니다(그 형태가 자기 게이트와 policy_lint를 통과하는지 테스트로
          고정했습니다). 같은 프롬프트 라이브 4회는 차단 **0/0/2/0**에 전부 repair
          없이 통과했고, 차단이 난 실행에서 gap이 **3→2로 줄었습니다** — 변경 전
          2→5로 늘던 회귀가 사라진 자리입니다. 다만 **변경 전 표본이 1회뿐이라
          차단 감소 자체는 아직 주장하지 않습니다.**
          상세: [docs/v1.11.17_builder_budget_and_input_gate.md](docs/v1.11.17_builder_budget_and_input_gate.md)

- ver 1.11.18 - **상호작용 축의 첫 검사.** 영수증 92건의 검사 조합 12가지 중 11가지가
          "플레이어가 어떻게 움직이는가" 한 축이었습니다. 흔한 상호작용 요청 10개를
          만들어 재보니 **10건 모두 기저 외 검사가 0개**였고, 그 공백을 기록하는
          미매핑 탐지기 자체가 새고 있었습니다 — `좌우`가 측정되는 어휘라는 이유로
          **적의 순찰**이 묵살되고, `보드`가 **"키보드"** 안에서 매칭돼 구현 지시문이
          요구 조항으로 잡히고 있었습니다(두 오탐이 서로를 가려 왔습니다). 거부권을
          어휘가 아니라 **검사 이름**에 묶는 방식으로 바꿔 탐지 8/10 → 10/10.
          라이브에서는 **코인 3개가 만들어졌는데 획득도 점수도 구현되지 않은 채
          `verified`**가 나왔고(검사 7개 전부 통과), 적 순찰은 기능이 되는데 보는
          검사가 없었습니다. 그래서 입력 없이 스스로 움직이는지 재는
          `autonomous_motion`을 넣었습니다 — 실측 Enemy 1.6458유닛(스크립트 speed 2 ×
          측정 0.8초와 일치). 접촉 대상 식별은 태그 대신 **마커 컴포넌트** 패턴을
          열었습니다.
          상세: [docs/v1.11.18_autonomous_motion_and_leaky_detector.md](docs/v1.11.18_autonomous_motion_and_leaky_detector.md)

- ver 1.11.19 - **정적 검사가 빌더 산출물을 보지 못했다.** `policy_lint`는 요청 문장이
          명시한 스크립트 경로만 검사하는데, 자연스러운 요청은 스크립트 경로를 말하지
          않습니다 — 그래서 **빌더가 스스로 만든 스크립트는 한 번도 검사된 적이
          없었습니다.** v1.11.18의 코인 실행이 `CompareTag("Coin")`을 담은 채
          `verified`로 나간 것이 그 결과입니다(프로젝트 태그가 비어 있어 트리거가
          걸리는 순간 예외를 던집니다). 기록된 실행 109개의 빌더 산출 스크립트
          158개를 재생하니 **위반 41건**이 나왔고, 현재 규칙이 다 들어간 7/29 이후로
          좁히면 78개 중 5건 — **전부 미정의 태그, 오탐 0**입니다. 검사 대상에 이
          세션이 쓴 스크립트를 넣고, 수정 안내는 "분기를 삭제하라"에서 **마커
          컴포넌트로 조건만 바꾸라**로 고쳤습니다(삭제하면 사용자가 요청한 기능이
          사라집니다).
          상세: [docs/v1.11.19_lint_scope_for_builder_scripts.md](docs/v1.11.19_lint_scope_for_builder_scripts.md)

- ver 1.11.20 - **라이브 확인 한 번이 결함 둘을 드러냈다.** v1.11.19를 실측하러 코인
          요청을 돌렸고 **2회 모두 미정의 태그를 잡았습니다** — 모델이 코인 로직을
          별도 파일로 빼도 검사 대상이 "세션이 쓴 스크립트 전체"라 빠져나가지
          못했습니다. 그 과정에서 ① repair 중 Ollama 호출이 끊기자 **이미 끝난
          측정이 영수증째 사라졌고**(기록된 166건 중 4건이 같은 방식으로 유실),
          ② repair가 태그 위반을 고쳤는데 **롤백이 위반이 남은 상태로 되돌렸습니다.**
          원인은 `player_movement_not_measured` 같은 **측정 공백을 결함으로 두 번
          센 것**이었고(첫 키가 이미 세고 있었습니다), 그래서 전부 blocked된 상태가
          더 좋아 보였습니다. 다중 사이클 영수증 53건을 재생해 **선택이 바뀐 것은
          1건 — 이 결함을 드러낸 그 실행뿐**입니다.
          상세: [docs/v1.11.20_receipt_survival_and_rollback_score.md](docs/v1.11.20_receipt_survival_and_rollback_score.md)

- ver 1.11.21 - **막힌 방향을 배치 문제로 판정합니다.** 시작 지점 옆에 플랫폼이 있으면
          그 방향 이동이 짧아지는데, 호스트가 그것을 `a_did_not_move_left`로 보고해
          **repair가 멀쩡한 스크립트를 고치러 갔습니다** — v1.11.15가 실측하고 안내로
          대응했다가 재발을 확인한 항목입니다. 기록된 좌우 동시 측정 **119건**의 비율
          분포를 보니 정상은 0.96~1.00, 막힘은 0.00~0.42이고 **그 사이가 비어 있어**
          임계값 0.5를 가정 없이 고를 수 있었습니다. 재생 결과 **108건 판정 유지 ·
          7건 오진 교체 · 4건 신규 적발 · 오탐 0**이고, `verified`가 `failed`로 뒤집히는
          영수증은 **하나**(D +2.06 / A −5.05로 막힌 배치가 통과했던 실행)입니다.
          양쪽이 같이 실패하는 입력 결함과 폭주 물리는 배제 조건으로 갈라 뒀습니다.
          상세: [docs/v1.11.21_movement_path_blocked.md](docs/v1.11.21_movement_path_blocked.md)

- ver 1.11.22 - **결함을 주입해 실패 경로를 실측했습니다.** 검사 세 개가 "탐지는 재생으로
          확인했지만 실제로 발동하고 repair가 고치는지는 안 봤다"로 남아 있었습니다.
          벽을 세우고(`movement_path_blocked`), 렌더러를 떼고(`player_has_no_renderer`),
          순찰 컴포넌트를 떼서(`no_object_moved_on_its_own`) 돌린 결과 **셋 다 탐지되고
          1사이클 repair로 복구**됐습니다. 특히 벽 주입에서 **repair가 스크립트를 한 줄도
          건드리지 않고 오브젝트를 위로 옮겼습니다** — v1.11.21이 겨냥한 바로 그
          행동입니다. `autonomous_motion` 재현성 표본도 3건(1.6458 / 1.7016 / 1.7001)이
          됐습니다. 코드 변경은 없습니다.
          상세: [docs/v1.11.22_injected_failure_paths.md](docs/v1.11.22_injected_failure_paths.md)

- ver 1.11.23 - **접촉 반응의 재료를 기록합니다(검사는 아직 아닙니다).** "코인에 닿으면
          점수" 같은 요청이 상호작용 10건 중 7건인데 재는 검사가 없었고, 막힌 이유는
          "호스트가 접촉을 일으킬 수 없다"로 적혀 있었습니다. **전제가 틀렸습니다** —
          이동·점프 측정이 이미 플레이어를 씬 안으로 데려갑니다. 남은 건 "안 사라졌을
          때 고장인지 안 닿았는지"의 비대칭 하나였습니다. 처음엔 경계 상자로
          `player_passed` 불린을 기록했는데 **첫 실측이 그 설계를 기각시켰습니다** —
          점프로 닿아 사라진 코인을 "안 닿았다"로, 2유닛 떨어진 코인을 "닿았다"로
          판정했습니다. 임계값을 굽는 대신 **최근접 거리**를 원자료로 남기자
          `0.071(사라짐) / 2.052 / 2.072`로 깔끔하게 갈렸습니다. 판정에는 들어가지
          않습니다.
          상세: [docs/v1.11.23_contact_candidate_recording.md](docs/v1.11.23_contact_candidate_recording.md)

- ver 1.11.24 - **거리 분포를 모아 보니 검사를 만들 때가 아니었습니다.** 요청 형태 넷을
          돌려 접촉 후보 15건을 모았는데(사라진 것 1건 0.071, 남은 것 14건 0.377~3.0),
          임계값을 고르지 못했습니다 — ① **후보의 절반이 상호작용 대상이 아니라**
          바닥·발판이고(늘 가까이 있으니 낮은 쪽을 채웁니다), ② `GoalFlag`처럼
          **사라지지 않는 게 정상**인 요청이 있고, ③ "위에서 밟으면"처럼 **호스트가
          만들지 않는 접촉**이 있습니다. ①의 재료로 후보마다 붙은 사용자 스크립트를
          기록에 넣었습니다(코인은 마커를 갖고 바닥은 갖지 않습니다). 분포가 아니라
          **분포를 읽을 수 없는 이유**가 이 사이클의 결과입니다.
          상세: [docs/v1.11.24_contact_distance_distribution.md](docs/v1.11.24_contact_distance_distribution.md)

- ver 1.11.25 - **점이 아니라 경로로 잽니다.** 접촉 거리 표본이 측정 구간의 **양 끝점만**
          담아, 코인 바로 아래를 통과하고도 거리가 2.052로 기록됐습니다(실제 수직 거리
          1.0) — A0가 잡으려는 "지나갔는데 반응이 없다"를 정면으로 막는 결함이었습니다.
          측정 구간을 선분으로 다루되, `restart_play`로 스폰에 되돌아가는 구간 사이를
          잇지 않도록 **연속 이동이 보장된 쌍만** 씁니다. 아울러 오프라인 복원으로 얻었던
          극적인 수치(`Item` 0.001)는 **시작 좌표를 가정한 허수**였음을 라이브로 확인해
          정정했습니다(실제 0.773~1.21). 그리고 이 수집 중 Ollama가 끊기면서
          **v1.11.20의 `repair_aborted`가 처음 발동해 측정 3건이 영수증에 보존**됐습니다.
          상세: [docs/v1.11.25_segment_distance_and_repair_abort.md](docs/v1.11.25_segment_distance_and_repair_abort.md)

- ver 1.11.26 - **모르는 인자를 버리고 성공을 돌려주고 있었습니다.** 접촉 표본을 찾다가
          아이템이 플레이어 경로 바로 위에 있는데도 획득되지 않는 씬을 만났습니다.
          획득 코드는 `CompareTag("Player")`인데 **Player의 태그가 `Untagged`**였고,
          모델이 태그를 설정하려 보낸 `unity_modify_gameobject {tag: "Player"}`는
          **그 도구에 `tag` 파라미터가 없어 조용히 버려지고 `{"status":"ok"}`가
          돌아왔습니다.** 이 세션에는 태그를 설정하는 도구가 아예 없어서, 기록된 실행
          중 **골인 판정·적 처치·아이템 획득 4건이 전부 같은 방식으로 죽어** 있었고
          호스트는 그것을 볼 검사가 없어 `verified`를 냈습니다. 이제 스키마에 없는
          인자는 **브리지에 보내지 않고 거절**하며, "실행되지 않았다"와 대안(마커
          컴포넌트)을 함께 알려줍니다.
          상세: [docs/v1.11.26_silent_argument_drop.md](docs/v1.11.26_silent_argument_drop.md)

- ver 1.11.27 - **접촉을 중심 거리로 재고 있었습니다.** A0에 남아 있던 "경로 바로 위의
          아이템이 0.773으로 기록된 이유"를 풀었습니다. 접촉은 중심이 아니라 표면에서
          일어나는데, 1×1×1 플레이어와 0.5짜리 코인은 **맞닿은 순간 중심 거리가
          0.75**입니다. 크기가 다른 오브젝트들의 중심 거리를 같은 자로 읽고 있었으니
          분포가 섞이는 것이 당연했고, 막힌 것은 표본 수가 아니라 단위였습니다.
          브리지가 이미 주던 `localScale`을 기록하고 **표면 간격(`surface_gap`)**을
          영수증에 남깁니다 — 반폭은 방향별로 재서 납작한 바닥이 분포를 오염시키지
          않게 했습니다. 실측 재생 결과 `Item` 0.773은 **닿기 0.023 전**이었고,
          같은 실행의 `motion_deltas.d`가 1초에 0.727밖에 못 간 이유가 그것이었습니다
          — 플레이어가 아이템에 **부딪혀 멈춰** 있었습니다. 씬 파일을 보니 콜라이더가
          트리거가 아닌데 `OnTriggerEnter`를 달아 놓아, 아이템이 획득 대상이 아니라
          벽으로 동작하고 있었습니다. 이어 돌린 **라이브 E2E가 A0이 15건을 모으고도
          만들지 못한 결정적 표본**을 첫 실행에서 냈습니다 — 아이템이 플레이어 시작
          지점에 생성돼 **표면 간격 −1.0(완전히 겹침)인데 사라지지 않았습니다.**
          같은 실행에서 v1.11.26의 새 거절이 처음 발동했고, 모델은 안내대로
          `CompareTag` 대신 `GetComponent` 식별로 넘어갔습니다. 그리고 트리거 배선
          결함을 기록된 실행에서 세어 보니 **접촉 요청 13건 중 6건이 트리거를 아무
          데도 만들지 않았고 그중 2건이 `verified`로 나갔습니다**(A4로 신규 등록).
          상세: [docs/v1.11.27_surface_gap.md](docs/v1.11.27_surface_gap.md)

- ver 1.11.28 - **사라진 것을 Play Mode 정지가 되돌리고 있었습니다.** 표면 간격 표본을
          모으러 같은 프롬프트를 6회 돌렸더니 아이템이 깊게 겹쳤는데 **6회 연속
          `disappeared: false`**였습니다. 씬 파일을 보니 트리거 배선은 멀쩡했고,
          결함은 측정 쪽이었습니다 — `measure_motion()`이 구간마다 Play Mode를
          정지·재시작하는데 **정지는 런타임 `Destroy()`를 되돌립니다.** 그런데 접촉
          상태는 모든 측정이 끝난 뒤 한 번만 찍고 있었습니다. 기록 전체에서
          `disappeared: true`가 하나뿐이었던 이유가 이것입니다 — 그 하나는 **점프로
          닿았고 점프가 마지막 구간**이었습니다. 이제 **정지 직전에** 훑고, 한 번
          사라진 표본은 덮어쓰지 않습니다. 재검증에서 **이동 구간의 소멸이 처음으로
          영수증에 남았습니다**(`gap −1.0, disappeared=True`). 곁들여 빌더 결함
          하나가 드러났습니다 — **트리거 콜라이더에 Rigidbody를 붙이면** 바닥과
          충돌하지 않아 아이템이 y −45까지 떨어집니다(A5로 신규 등록).
          상세: [docs/v1.11.28_disappearance_erased_by_restart.md](docs/v1.11.28_disappearance_erased_by_restart.md)
