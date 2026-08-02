# 시연용 히어로 프롬프트 — 2.5D 횡스크롤 스테이지 (2026 공모전)

공모전 시연 영상에서 "검증 영수증"이 아니라 **실제로 게임처럼 보이는 스테이지**를 보여주기 위한
프롬프트와 실행 절차. 산출물은 `prompts/solo_ai_demo01.txt`(히어로)와
`prompts/solo_ai_demo_safe.txt`(안전판)이다. 저장소 코드는 변경하지 않는다.

## 1. 출발점 — 기존 대형 프롬프트는 통과한 적이 없다

`docs/side_scroller_level01_orchestration_prompt.md`와 `prompts/platformer25d_mvp.txt`로
실행된 영수증은 `logs/receipts/2026/07/22~23/`에 12건 남아 있고 **전부 `failed`** 다.
45초~745초를 쓰고 `policy_lint:*`와 `blocked:*:policy_lint_failed`로 끝난다.
반면 짧은 프롬프트는 60~90초에 통과한다(점프 형태 통산 13/13).

그래서 이 문서의 프롬프트는 더 상세한 명령서가 아니라, **그 12건이 실패한 원인을 코드 수준에서
하나씩 제거한 짧은 요청**이다.

## 2. 제거한 실패 원인 4가지

### ① 부스트 사양이 임계값과 수학적으로 충돌

`prompts/platformer25d_mvp.txt`는 "부스트 지속시간 0.18초, 속도 2배"를 요구한다.
호스트는 `verification.py:343` `boost_duration = 0.5` — D를 0.5초, D+LeftShift를 0.5초 누른
거리를 비교하고 `boost_min_ratio = 1.4` 이상을 요구한다(`verification.py:987-989`).

0.18초만 2배면 그 창의 거리는 `0.18×2v + 0.32×v = 0.68v`, 일반은 `0.5v` → **비율 1.36 < 1.4**.
구현이 완벽해도 떨어진다. 영수증의 `boost_distance_too_short`가 이것이다.

→ 부스트는 **Shift를 누르고 있는 동안 계속 유지되는 속도 배수**여야 한다. 지속시간 제한도
쿨다운도 없이. 배수 2.5배는 허용 구간 1.4~10.0의 한가운데다.

### ② policy_lint는 요청 문구에 의해 켜진다

`policy_lint.py:77-88`의 규칙은 **요청문에 특정 문자열이 있을 때만** 발동한다.

| 요청문에 이 문구가 있으면 | 켜지는 위반 |
|---|---|
| `낙사 시 시작 위치로 복귀` | `fall_respawn_check_missing` |
| `무입력 0.5초` | `idle_velocity_not_zeroed` |

기존 MVP 프롬프트는 두 문구를 그대로 담았고, 영수증 4건이 이 위반으로 **모든 검사가 blocked** 되며
통째로 실패했다. 낙사 복귀는 요청할 필요도 없다 — `templates/LevelLoader.cs:215-224`의
`LevelLoaderHazardZone`이 hazard에 닿은 Player를 `RespawnPlayer()`로 자동 복귀시킨다.

### ③ 검증 이동 거리가 안전 평지보다 길다

호스트가 굴리는 입력은 D 1.0초 → A 1.0초 → 점프 → 부스트 측정 D 0.5초 + D+Shift 0.5초다
(`agent.py:840-846`). 이동 속도 7 기준 오른쪽으로 누적 16유닛 이상 나간다.
기존 프롬프트의 "오른쪽 8유닛 평지"로는 부족하다.

`docs/v1.11.15_camera_observation_and_boost_cap.md` §5가 기록한 "코드를 고쳐서는 절대 통과할 수
없다"는 실패 모드가 이것이다. → **시작 평지를 오른쪽 22유닛까지 확보**하고 첫 장애물을 x 24부터 둔다.
마리오 1-1도 긴 평지로 시작하므로 게임성과 충돌하지 않는다.

### ④ 검사 어휘 밖의 낱말은 화면에 ⚠로 뜬다

`verification.py:141` `_REQUIREMENT_MARKERS`에 걸리면 `unmapped_requirements`가 되어
"⚠ 요청에 있으나 측정하지 않은 항목"으로 출력된다. 판정은 통과지만 화면이 지저분해진다.

회피: `점수` `클리어` `스폰` `~에 닿으면` `사라지` `숫자+개/층/칸/줄/단` `격자` `순찰`.
한글 수사("두 개")는 `\d+`에 걸리지 않아 안전하고, 좌표의 숫자(`x 24`, `2.5배`)도 안전하다.

## 3. 정적 추출 결과 (측정값)

```
requested_checks : gameplay, movement, bidirectional, jump, jump_landing,
                   camera_follow, camera_fixed_z, player_constraints, boost,
                   level_marker, screenshot, components:Main Camera, components:Player
move keys        : d / a  (explicit=True)
jump key         : space
UNMAPPED         : []          ← ⚠ 줄이 뜨지 않는다
```

정책 쪽 확인:

```
레벨 워크플로 허용            : True  ("데이터 주도")
PlayerInput 워크플로(원치 않음): False
policy_lint 트리거 문구       : 4개 모두 False
빈 씬 게이트 문구             : 모두 False ("새 씬"만 사용)
대상 씬/레벨 파일 기존 존재     : 모두 False
```

의도적으로 뺀 검사:
- `left_boost` — `"a+leftshift"` 리터럴이 있어야 켜진다(`verification.py:464`). 검사 하나를 아낀다.
- `idle_stability` — `"무입력 0.5초"`가 policy_lint까지 같이 켠다.

## 3.5 1차 실행 결과 — 통과했지만 플레이어가 보이지 않았다

2026-07-31 03:33, 영수증 `logs/receipts/2026/07/31/20260731_033338_195_471258ba7b.json`:

```
status           : verified
elapsed_seconds  : 286.672
measured_checks  : 13개 전부
skipped_checks   : []
failures         : []
unmapped_requirements : []
```

**복합 레벨 요청이 통과한 첫 사례다.** 그런데 Game View 스크린샷에 플레이어가 없었다.
씬 파일의 컴포넌트를 세면 원인이 나온다.

```
1 BoxCollider · 1 Camera · 1 Light · 1 Rigidbody · 4 MonoBehaviour
MeshRenderer: 0    MeshFilter: 0
```

Player가 **메시 없는 빈 GameObject**였다. Rigidbody와 BoxCollider와 스크립트만 붙어 있어
물리는 완벽히 동작했고 — 그래서 이동·점프·부스트·카메라 추종이 전부 실측 통과했다 —
화면에 그릴 것이 없었다.

원인 두 가지:

1. **프롬프트 결함.** 통과 이력이 있는 프롬프트는 전부 `Player(캡슐)를 만들어`라고 쓴다.
   히어로 프롬프트를 새로 쓰면서 그 괄호가 빠졌다. `prompts/solo_ai_demo_safe.txt`에는 남아 있다.
2. **하네스 공백.** `verification.py:394`의 `components["Player"]`는 `["Rigidbody", "Collider"]`만
   요구한다. **렌더러를 보는 검사는 어디에도 없다.** 보이지 않는 플레이어에 `verified`가 찍힌다.
   이 저장소가 v1.11.13(빈 집합 성공), v1.11.14(부분을 전체로 보고), v1.11.15(1인칭 카메라를
   추종으로 오인)에서 잡아온 것과 같은 계열의 결함이며, **아직 열려 있다.**

프롬프트 쪽 대응(적용 완료):

- `Player는 Capsule 프리미티브로 만들고 ... 빈 GameObject에 Collider만 붙이지 마라 —
  화면에 실제로 보이는 메시가 있어야 한다`
- `눈에 잘 띄는 빨간색 머티리얼을 만들어 입힌다` (갈색 지면·주황 블록과 구분)
- `Player의 Z 위치는 -1로 두어 레벨 블록보다 카메라 쪽에 오게 한다`
  (레벨 오브젝트 Z 두께 2 → z −1~+1, 플레이어 앞면 z −1.5 → 파이프에 가려지지 않음)

같은 실행에서 드러난 레벨 품질 문제도 함께 보정했다. 생성된 레벨은 x 28부터 52까지
**한 방향으로만 높아지는 긴 계단**이었고(구간 구분이 뭉개짐), 파이프가 계단 안에 박혔고,
모든 오브젝트의 Z 두께가 1이라 납작한 판으로 보였고, 낭떠러지가 지면을 끊은 게 아니라
작은 빨간 판이었다. 대응 문구 4줄을 `[레벨 배치]`에 추가했다.

## 4. 실행 설정

```bash
UNITY_AGENT_MAX_ITERS=45 uv run main.py
```

- `MAX_ITERS` 기본값 30은 `docs/improvement_report_2026-07-30.md` §5 기준
  **`builder_stage_evaluated` 38건 중 37건이 정확히 그 한도에서 잘렸다.** 이 요청은 평소보다
  크므로 45로 올린다(`config.py:22`).
- **`UNITY_AGENT_PLANNER=always`는 쓰지 않는다.** 대형 명령서 문서가 권장하지만 플래너 경로는
  v1.11.x 신뢰도 수치가 하나도 없는 미측정 경로이고, 실패한 영수증들이 그 경로다. 기본 `auto` 유지.
- Unity Editor를 먼저 띄우고 Console에 `[McpBridge] Listening` 확인.

## 5. 촬영 전 검증

### 5.1 정적 추출 (1초 미만)

프롬프트를 손댈 때마다 **먼저** 돌린다. E2E는 60~200초, 이건 1초 미만이다
(`docs/why_user_prompts_found_more_origin_docs.ipynb` §5.2).

```bash
uv run python -c "import sys; from verification import VerificationSpec; s=VerificationSpec.from_request(open(sys.argv[1],encoding='utf-8').read()); print('검사:', s.requested_checks()); print('이동키:', s.move_right_key, s.move_left_key); print('미매핑:', s.unmapped_requirements())" prompts/solo_ai_demo01.txt
```

합격 조건: `boost`·`jump_landing`·`camera_follow`·`level_marker`가 모두 있고, 이동키가 `d`/`a`이고,
**미매핑이 비어 있을 것**.

### 5.2 E2E 리허설 3회

씬 이름만 `SoloAiDemo01` → `02` → `03`으로 바꿔 3회. 같은 씬 재사용 금지(신선 씬 E2E 관례).

- 영수증 `status`가 `verified`이고 `measured_checks`가 비어 있지 않을 것
  (`AGENTS.md` §6: `measured_checks`가 비어 있는 `verified`는 무효)
- `failures`에 `boost_distance_too_short` / `player_did_not_move_right`가 없을 것
- Console에 `[LevelLoader] Loaded soloAiDemo01: ...` 실제 출력
- Game View 스크린샷에서 캡슐이 넘어져 있지 않고 카메라가 플레이어를 담고 있을 것
- **사람이 직접 플레이해 끝까지 갈 수 있는지 확인** — 게임성 판정 지점

3회 중 2회 이상 통과하면 본 촬영. 통과 회차의 씬은 성공 테이크로 보존한다.

### 5.3 실패 시 조정

| 증상 | 조치 |
|---|---|
| `boost_distance_too_short` | 배수 2.5 → 3.0 (상한 10.0까지 여유) |
| `player_did_not_move_right` / `d_did_not_move_right` | 시작 평지를 x 22 → x 26으로 연장 |
| `player_jumped_too_high` / `player_did_not_land` | "한 번 누를 때 한 번만 떠올라" 문장 확인, 점프 높이 약 3유닛 명시 |
| `camera_is_player_viewpoint` | `Vector3(0, 2, -10)` 오프셋 문장 확인. 거리 1.5유닛 미만이면 실패 |
| `policy_lint:*`로 전부 blocked | §2②의 두 문구가 다시 들어갔는지 확인 |
| 레벨이 밋밋함 | 좌표 가이드는 유지하고 색상 지시를 구체화. 배치를 통째로 모델에 맡기지 말 것 |

## 6. 안전판

히어로 프롬프트는 복합 요청이라 실측 재현성이 1/5 구간이다
(`docs/v1.11.15_camera_observation_and_boost_cap.md` §7: "복합 요청의 빌더 완결성은 2차 1회뿐이다").
촬영 당일 3회 연속 실패하면 `prompts/solo_ai_demo_safe.txt`로 갈아탄다 — 점프 형태 통산 13/13,
약 60초, 알려진 미해결 결함 없음. 검사는 6개(`gameplay, movement, bidirectional, jump,
jump_landing, components:Player`)로 줄지만 확실히 찍힌다.

## 7. 남은 위험 (정직하게)

- **복합 요청 재현성 1/5.** 정적 추출과 정책 게이트는 통과를 확인했지만, 이는 "요청이 올바르게
  해석된다"는 뜻이지 "모델이 매번 올바른 코드를 쓴다"는 뜻이 아니다. 리허설 없이 촬영하지 말 것.
- **레벨 배치 품질은 모델이 채운다.** 구간과 X 범위는 사람이 정했지만 실제 간격·높이는 모델이
  정한다. 점프로 못 넘는 배치가 나오면 자동 검증은 이를 잡지 못한다 — 사람이 플레이해서 확인하는
  단계(§5.2 마지막 항목)가 유일한 방어선이다.
- **레벨 오브젝트 수 검사는 없다.** `docs/HANDOFF_2026-07-29.md` §3이 기록한 대로 구조·개수를
  보는 검사는 어휘에 없다. 스테이지가 절반만 생성돼도 `verified`가 나올 수 있다.
