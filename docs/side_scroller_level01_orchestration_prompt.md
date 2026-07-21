# 2.5D 횡스크롤 스테이지 제작 명령서

이 문서는 `unity_local_mcp`의 로컬 모델에게 고전적인 첫 스테이지 감각의
독창적인 2.5D 횡스크롤 플랫포머를 제작하도록 지시하기 위한 실행 명령서다.

특정 상용 게임의 캐릭터, 로고, 텍스처 또는 레벨을 그대로 복제하지 않는다.
Unity 기본 프리미티브와 단색 머티리얼을 사용하며, 실제 플레이 가능성과 자동
검증을 최우선으로 한다.

## 1. 권장 실행 설정

PowerShell에서 `unity_local_mcp` 디렉터리로 이동한 뒤 다음과 같이 실행한다.

```powershell
$env:UNITY_AGENT_PLANNER="always"
$env:UNITY_AGENT_MILESTONE_ITERS="16"
$env:UNITY_AGENT_PLAN_TOTAL_ITERS="80"
uv run main.py
```

권장 설정의 목적:

- `PLANNER=always`: 포괄적인 게임 제작 요청을 반드시 마일스톤으로 분해한다.
- `MILESTONE_ITERS=16`: 씬 구성과 검증 직전에 도구 예산이 끝나는 상황을 줄인다.
- `PLAN_TOTAL_ITERS=80`: 재시도와 최종 플레이 검증에 사용할 전체 예산을 확보한다.

Unity Editor는 대상 프로젝트와 함께 미리 실행해 둔다. Console에
`[McpBridge] Listening` 로그가 표시되는지 확인한다.

## 2. 1차 명령: 플레이 가능한 핵심 스테이지 제작

아래 내용을 `unity_local_mcp` CLI에 한 번의 요청으로 입력한다.

```text
새 씬 Assets/Scenes/SideScrollerLevel01.unity에 고전적인 첫 스테이지 감각의
독창적인 2.5D 횡스크롤 플랫포머를 만들어줘.

중요: 기존 씬을 재사용하지 말고 새 씬에서 작업해라. 닌텐도 캐릭터, 로고,
텍스처나 원본 레벨을 그대로 복제하지 말고 Unity 기본 프리미티브와 단색
머티리얼만 사용해라.

[게임 구조]
- 플레이 방향은 X축이며 Z축 이동은 금지한다.
- Player는 Capsule이고 이름은 정확히 "Player"로 한다.
- 조작은 A/D 또는 좌우 방향키 이동, Space 점프다.
- 새 Input System만 사용하고 Rigidbody 3D 물리를 사용한다.
- Rigidbody2D 및 legacy UnityEngine.Input API는 사용하지 마라.
- PlayerMovement.cs의 Awake에서 FreezeRotation과 FreezePositionZ를 적용해
  플레이어가 넘어지거나 Z축으로 이탈하지 않게 해라.
- 떨어지거나 hazard에 닿으면 현재 레벨 시작 위치로 리스폰한다.
- 목표 지점에 도착하면 클리어 로그를 출력한다.

[레벨 구성]
canonical LevelLoader를 설치하고
Assets/StreamingAssets/Levels/sideScroller01.json을 사용해라.
레벨은 왼쪽에서 오른쪽 순서로 다음 구성을 가져야 한다.

1. 조작을 익히는 넓고 안전한 시작 지면
2. 점프로 올라갈 수 있는 낮은 블록 3개
3. 장식용 노란 블록과 코인 모양 오브젝트
4. 높이가 다른 초록색 파이프 형태의 플랫폼 2개
5. 짧고 안전하게 넘을 수 있는 첫 번째 낭떠러지
6. 계단형 플랫폼 구간
7. 두 번째 낭떠러지와 작은 hazard 구간
8. 마지막 계단과 명확하게 보이는 초록색 Goal

플랫폼 사이 거리는 Player의 moveSpeed와 jumpForce로 실제 통과할 수 있게 설계해라.
첫 번째 테스트에서는 움직이는 플랫폼이나 복잡한 전투 시스템을 넣지 마라.
먼저 처음부터 끝까지 통과 가능한 플랫폼 구조를 완성하는 것이 최우선이다.

[카메라]
Assets/Scripts/SideScrollerCamera.cs를 작성하고 Main Camera에 부착해라.
- Player를 X축으로 부드럽게 추적한다.
- Y와 Z는 고정하거나 완만하게만 따라간다.
- 플레이어가 화면 중앙보다 약간 왼쪽에 보여 앞쪽 길이 더 많이 보여야 한다.
- Orthographic 카메라를 사용해 고전 횡스크롤 느낌을 만든다.
- 시작 지점 뒤쪽으로 카메라가 이동하지 않게 한다.

[시각 구성]
- 지면과 플랫폼은 밝은 갈색 또는 주황색
- 파이프와 Goal은 초록색
- hazard는 빨간색
- 장식 블록은 노란색
- 배경은 밝은 하늘색
- 모든 오브젝트가 게임 카메라에서 명확히 구분되어야 한다.

[필수 산출물]
- Assets/Scenes/SideScrollerLevel01.unity
- Assets/Scripts/PlayerMovement.cs
- Assets/Scripts/SideScrollerCamera.cs
- Assets/Scripts/LevelLoader.cs
- Assets/StreamingAssets/Levels/sideScroller01.json

[완료 검증]
1. 스크립트 compile error가 0인지 확인한다.
2. 씬을 저장한다.
3. 플레이 모드에서 [LevelLoader] Loaded 로그를 실제로 확인한다.
4. Player 위치를 조회한 뒤 rightArrow를 짧게 입력하고 다시 조회하여 X좌표가
   실제로 증가했는지 확인한다.
5. Space 입력 전후 Player Y좌표를 비교하여 점프가 실제로 동작하는지 확인한다.
6. 이동 전후 Main Camera X좌표를 비교하여 카메라가 Player를 따라가는지 확인한다.
7. error/exception이 없는지 확인한다.
8. 실행 중 게임 뷰 스크린샷을 찍는다.
9. 반드시 플레이 모드를 종료한다.

작업을 4~5개 마일스톤으로 나누고, 각 마일스톤의 산출물과 검증을 끝낸 후
다음 단계로 진행해라. 모델의 완료 선언보다 실제 컴파일, 콘솔 로그와 좌표 변화를
기준으로 판단해라.
```

## 3. 2차 명령: 적과 시각적 폴리싱

1차 실행이 성공하고 이동·점프·카메라 검증이 통과한 뒤에만 입력한다.

```text
Assets/Scenes/SideScrollerLevel01.unity의 기존 플레이 가능성과 검증 결과를
보존하면서 폴리싱해줘.

- 단순 왕복 이동하는 적 2마리를 추가한다.
- 적과 충돌하면 Player가 시작 위치로 리스폰한다.
- 코인과 노란 블록의 시각적 배치를 개선한다.
- 시작 지점, 중간 구간, Goal이 한눈에 구분되게 색과 조명을 조정한다.
- 기존 점프 거리와 플랫폼 통과 가능성을 깨뜨리지 마라.
- 기존 PlayerMovement와 SideScrollerCamera를 새로 갈아엎지 말고 필요한 부분만
  수정해라.
- 완료 후 이동, 점프, 적 충돌, 카메라 추적, 런타임 오류와 스크린샷을 다시
  검증해라.
- 검증이 끝나면 반드시 플레이 모드를 종료해라.
```

## 4. 성공 판정 기준

다음 항목이 모두 충족되어야 완료로 본다.

| 항목 | 필수 증거 |
|---|---|
| 컴파일 | `unity_read_console`의 error/exception 0 |
| 레벨 로딩 | 실제 콘솔 결과에 `[LevelLoader] Loaded` 포함 |
| 이동 | 입력 전후 Player X좌표가 `0.001` 이상 변화 |
| 점프 | Space 입력 후 Player Y좌표 상승 확인 |
| 카메라 | Player 이동 방향과 Main Camera X좌표 변화 방향이 동일 |
| 씬 저장 | `Assets/Scenes/SideScrollerLevel01.unity` 파일 존재 |
| 화면 | 실행 중 game view 스크린샷 경로 존재 |
| 종료 상태 | `unity_get_state`에서 `isPlaying=false` |

모델이 “정상 작동한다”고 서술한 것만으로는 성공으로 판정하지 않는다. 실제 도구
결과에 좌표, 로그와 파일 경로가 있어야 한다.

## 5. 실패 시 재명령 예시

### Player가 움직이지 않는 경우

```text
입력 검증에서 Player X좌표가 변하지 않았다. PlayerMovement.cs와 Player의
Rigidbody/컴포넌트 구성을 확인해라. Rigidbody 3D와 새 Input System만 사용하고,
수정 후 위치 조회 → rightArrow 입력 → 대기 → 위치 재조회 순서로 다시 검증해라.
```

### Player가 넘어지거나 Z축으로 벗어나는 경우

```text
Player가 이동 중 회전하거나 Z축으로 이탈한다. PlayerMovement.cs의 Awake에서
RigidbodyConstraints.FreezeRotation과 RigidbodyConstraints.FreezePositionZ를
코드로 적용해라. 임의의 enum 숫자를 추측하지 마라. 수정 후 다시 플레이 검증해라.
```

### 카메라가 따라오지 않는 경우

```text
Main Camera가 Player를 따라오지 않는다. SideScrollerCamera의 target이 실제
Player Transform인지 확인하고, 이동 전후 Player와 Main Camera의 X좌표를 각각
조회해 비교해라. 카메라는 LateUpdate에서 X축을 부드럽게 추적해야 한다.
```

### 레벨 로그가 확인되지 않는 경우

```text
error 로그만 읽고 완료 처리하지 마라. 플레이 후 unity_read_console을 types 필터
없이 호출하여 실제 [LevelLoader] Loaded 로그를 확인해라. 로그가 없으면
LevelLoader 오브젝트, levelFile 값과 JSON 파일 경로를 수정해라.
```

### 도구 호출 한도에 도달한 경우

```text
이전 시도에서 생성된 파일과 씬을 재사용해라. 이미 성공한 작업을 다시 만들지 말고,
현재 누락된 검증 항목만 수행해라. 씬 변경은 플레이 모드 진입 전에 확인하고 저장해라.
```

## 6. 운영 권장사항

- 첫 실행에서는 플레이 가능한 핵심 루프만 만든다.
- 적, 코인 동작, 파티클과 UI는 2차 요청으로 분리한다.
- 긴 입력 홀드는 Player가 플랫폼 밖으로 떨어질 수 있으므로 이동 확인에는
  `rightArrow tap`과 짧은 대기를 사용한다.
- 씬 확인과 저장을 먼저 수행한 뒤 플레이 모드로 진입한다.
- 플레이 검증이 실패하면 반드시 플레이 모드를 종료한 뒤 스크립트나 씬을 수정한다.
- v1.8부터 모든 실행은 `logs/runs/YYYY/MM/DD/`에 `.log`와 `.jsonl`로 자동 저장된다.
  CLI에서 `/log`를 입력하면 마지막 실행 로그 경로를 다시 확인할 수 있다.
