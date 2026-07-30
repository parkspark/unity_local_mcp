# AGENTS.md — unity_local_mcp 작업 규칙

로컬 LLM(Ollama `qwen3-coder:30b`)으로 Unity Editor를 제어하는 에이전트 CLI.
현재 v1.11.12. 이 저장소에서 작업하는 모든 에이전트는 아래 규칙을 따른다.

---

## 1. 역할 분담 — 가장 중요

**이 저장소의 산출물은 로컬 모델이 돌리는 에이전트다. 너는 그 엔진을 만드는 쪽이지
실행하는 쪽이 아니다.**

| | 담당 |
|---|---|
| 구조·코드 개선 (`agent.py`, `verification.py`, 브리지 C# 등) | 너 (Codex/Claude) |
| 사용자 명령의 실제 실행 — 게임 제작, 도구 호출 | 로컬 모델 `qwen3-coder:30b` |

**E2E 검증은 반드시 로컬 모델이 직접 도구를 호출하게 한다.**

```bash
uv run python main.py --prompt-file <파일>
```

네가 `unity_*` MCP 도구를 대신 호출해 검증하면, 로컬 모델이 실전에서 겪는 문제
(tool-call 누수, 경로 실수, 컨텍스트 소진, 검증 회피)를 전혀 재현하지 못하고
**네가 완벽하게 조작한 해피패스**만 확인하게 된다. 이 프로젝트가 v1.11.2부터 싸워온
"측정하지 않은 것을 성공으로 판정" 문제를 그대로 재생산하는 것이다.

네가 `unity_*`를 직접 호출해도 되는 경우는 **두 가지뿐**이다.

1. **브리지 배관 자체의 결정적 확인** — 예: `send_key`가 KeyboardState를 큐잉하는지
2. **사전 준비 / 사후 정리** — 씬 상태 확인, 테스트로 주입한 결함 복원

**검증 판정을 대신 만들어내지 마라.** 판정은 호스트 검증 계층(`verification.py`)이 한다.

---

## 2. Git 정책

**커밋과 푸시는 항상 사람이 직접 한다. 에이전트는 요청받아도 하지 않는다.**
`git commit`도 `git push`도 실행하지 말고, 커밋 단위를 제안하지도 마라. 작업이 끝나면
변경 파일과 내용만 보고한다. `git status` · `git diff` · `git log` 같은 읽기 명령은 쓴다.

---

## 3. 버전 문서 규칙

버전을 올리면 **반드시** 함께 한다.

1. `version.py`의 `__version__` 갱신
2. `docs/vX.Y.Z_<주제>.md` 작성
3. `README.md` 버전 히스토리에 항목 추가 + `상세:` 링크

문서 구성: **배경**(왜 고쳤는지) → **변경 내용** → **검증**(테스트 수 전후, 실측 대조표)
→ **남은 과제**.

**실측하지 않은 부분은 "단위 테스트로만 검증했다"고 명시한다.** 이 저장소의 문서는
실측 증거(영수증 경로, 측정값 전후)를 보존하는 역할을 하므로, 검증 범위를 부풀리면
문서의 목적이 무너진다.

---

## 4. 실행 전제 조건

- **Unity Editor가 열려 있어야 한다.** Console에 `[McpBridge] Listening` 확인
- **Ollama 실행 중** — `ollama serve`, `qwen3-coder:30b` 필요
- 프로젝트 경로: `--project` 인자 또는 `UNITY_PROJECT_DIR` 환경변수
  (현재 작업 대상: `C:\Users\park\My project (55)`)

---

## 5. 표준 명령

```bash
# 테스트 (현재 189개 통과, subtest 20개 통과)
uv run python -m pytest tests/

# 로컬 모델 E2E — 빌더부터 전체
uv run python main.py --prompt-file <파일>

# 기존 산출물의 실패 항목만 수정·재검증 (빌더 생략)
uv run python main.py --prompt-file <파일> --repair-existing

# 대화형
uv run python main.py
```

E2E는 오래 걸리므로 백그라운드 실행을 권한다
(`UNITY_AGENT_TASK_TIMEOUT` 기본 1800초).

---

## 6. 결과 확인 위치

**검증 영수증** `logs/receipts/YYYY/MM/DD/*.json`

| 필드 | 의미 |
|---|---|
| `status` | `verified` / `failed` |
| `requested_checks` | 요청된 검증 항목 |
| `measured_checks` | **실제로 측정된** 항목 |
| `skipped_checks` | 요청됐으나 측정 못 한 항목 |
| `attempts` | 사이클별 실패 이력 (`verify` / `reverify` / `rollback`) |
| `evidence` | 좌표 델타, 컴포넌트, 런타임 오류 등 원측정값 |

> **`measured_checks`가 비어 있는 `verified`는 무효다.** v1.11.2 이전에는 이 저장소의
> `verified` 영수증 3건이 전부 Play Mode를 돌리지 않은 공집합 성공이었다.

**실행 로그** `logs/runs/YYYY/MM/DD/*.jsonl` — 이벤트 단위 기록.
`verification_failure_delta`, `verification_rollback`, `verification_best_state`,
`movement_key_fallback` 등을 보면 루프가 왜 그렇게 판단했는지 추적할 수 있다.

---

## 7. 아키텍처

```
main.py (CLI) → planner.py (큰 요청 → 마일스톤 분해)
              → agent.py (ReAct 루프) ⇄ Ollama
                 ↕ task_contract.py (실행 전 정책 게이트)
                 ↕ verification.py (호스트 독립 검증 · 실패 판정)
              → mcp_client.py → unity_mcp/server.py → TCP → UnityMcpBridge.cs
```

| 모듈 | 역할 |
|---|---|
| `agent.py` | ReAct 루프, 검증 오케스트레이션, repair 사이클, rollback |
| `verification.py` | 요청 → 검증 명세 추출, 실패 판정, 영수증 |
| `task_contract.py` | 실행 전 정책 게이트 (경로 제한, 위험 도구 차단) |
| `planner.py` | 대규모 요청 마일스톤 분해, 산출물 대장 |
| `snapshot.py` | repair rollback용 파일 스냅샷 |
| `mcp_client.py` | MCP stdio 세션, 컴파일 대기, 포트 추적 재접속 |
| `local_tools.py` | 호스트 직접 실행 파일 도구 (스크립트·레벨 쓰기) |
| `policy_lint.py` / `preflight.py` | 정적 검사, 요청 사전 검사 |

---

## 8. 최근 변경

| 버전 | 요약 | 문서 |
|---|---|---|
| v1.11.1 | 시작 4단계 상태 표시 | [문서](docs/v1.11.1_status_and_improvement_report.md) |
| v1.11.2 | **검증 공집합 성공 결함(P0) 수정** — 행동 키워드를 게임 키워드와 독립 감지 | [문서](docs/v1.11.2_verification_spec_reliability.md) |
| v1.11.3 | repair 회귀 오판 수정 — 최초 측정을 회귀로 보던 문제 | [문서](docs/v1.11.3_repair_regression_detection.md) |
| v1.11.4 | 이동 검증 공백 — 속도 상한 + 요청 키 사용. **최초의 정당한 verified** | [문서](docs/v1.11.4_movement_verification_gaps.md) |
| v1.11.5 | repair snapshot/rollback | [문서](docs/v1.11.5_repair_rollback.md) |
| v1.11.6 | 점프 키 명세, 개발 의존성 정리 | [문서](docs/v1.11.6_jump_key_and_dev_deps.md) |
| v1.11.7 | 엄격한 새 씬 전체 E2E 첫 통과, 검증 배관 신뢰성 보강 | [문서](docs/v1.11.7_fresh_scene_e2e_reliability.md) |
| v1.11.8 | **빌더 자체 완결성** — 첫 호스트 검증 무수정 통과, 직접 키보드 정책·저장 장벽 | [문서](docs/v1.11.8_builder_stage_completeness.md) |
| v1.11.9 | 점프 상한 검증(`player_jumped_too_high`), rising edge 래치, 정책 게이트의 스크립트 대필 제거. 점프 +19.96 → **+4.98 · 착지 true**. 대가로 `build_stage_success` 후퇴 | [문서](docs/v1.11.9_bounded_jump_and_unrewritten_scripts.md) |
| v1.11.10 | 정책 게이트 오탐 제거 — 접지 판정을 철자에서 결함 조건으로, 헬퍼 호출 추적, 차단 메시지에 코드 첨부, 무의미한 재작성 차단. **`build_stage_success=true` · `attempts=1` 회복** | [문서](docs/v1.11.10_gate_false_positive.md) |
| v1.11.11 | **재현성 측정** — 새 씬 E2E **9/9** `build_stage_success=true`(repair 0회, 접지 오탐 0회). 그중 3회는 남은 스크립트를 지우고 시작. 스니펫 없는 항목의 빈 remedy 헤더 제거 | [문서](docs/v1.11.11_reproducibility_and_empty_remedy.md) |
| v1.11.12 | **요청 형태 일반화** — 카메라 추종 요청에서 빌더 완결성 0/3. 콘솔 분류(런타임→컴파일 오계산), 태그 게이트 범위, 빈 씬 템플릿 차단을 고쳐 **3/3 회복**, 점프 형태 11/11 무회귀 | [문서](docs/v1.11.12_request_shape_generalization.md) |

**현재 진행 상황과 다음 작업**: [docs/HANDOFF_2026-07-29.md](docs/HANDOFF_2026-07-29.md)
