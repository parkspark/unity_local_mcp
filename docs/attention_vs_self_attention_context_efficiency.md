# Attention과 Self-Attention을 이용한 콘텍스트 절약 연구 메모

작성일: 2026-07-29  
대상: `qwen3-coder:30b` 기반 `unity_local_mcp`

## 1. 결론

**Attention과 self-attention의 이름 차이만으로 콘텍스트 토큰이 줄어들지는 않는다.**
현재 qwen3-coder 같은 decoder-only LLM에 긴 문서를 프롬프트로 넣으면 모든 토큰이
self-attention 대상이다. 실제 절약 방법은 다음 두 가지다.

1. **즉시 적용 가능:** 필요한 기록만 검색·요약해 프롬프트 토큰 수 자체를 줄인다.
2. **모델 구조 변경 필요:** 긴 원문은 외부 메모리나 encoder에 두고, 작은 latent 또는
   검색 결과에 cross-attention한다.

이 저장소에는 이미 fresh context와 산출물 대장 방식이 있으므로, 우선순위는
**“전체 대화 이월”을 “결정적 상태 요약 + 관련 기록 top-k 검색”으로 바꾸는 것**이다.

## 2. Attention과 Self-Attention의 차이

Attention은 query가 key와의 관련도를 계산하고 value를 가중합하는 일반 연산이다.

```text
Attention(Q, K, V) = softmax(QKᵀ / √d) V
```

| 구분 | Q 출처 | K/V 출처 | 용도 |
|---|---|---|---|
| Self-attention | 현재 토큰열 | 같은 토큰열 | 문장·대화 내부 관계 계산 |
| Cross-attention | 생성기 또는 작은 latent | 다른 문서·encoder·외부 메모리 | 필요한 외부 정보 선택 |

Transformer decoder의 self-attention은 길이 `n`인 전체 프롬프트를 서로 비교하므로
표준 구현의 계산·attention-map 메모리는 대략 `O(n²)`이다. 반면 원문 `n`개를 작은
latent `r`개로 압축하는 cross-attention은 핵심 구간을 `O(nr)`로 만들 수 있다
(`r << n`). Perceiver가 이 비대칭 attention과 latent bottleneck을 사용한다.

다만 **Ollama에 완성된 qwen3-coder 모델을 그대로 띄우는 현재 구조에는 새
cross-attention 층을 프롬프트만으로 추가할 수 없다.** 따라서 이 차이는 당장 모델
구조를 바꾸라는 뜻이 아니라, 호스트가 외부 메모리와 검색기를 맡아 qwen에 전달하는
self-attention 토큰을 줄이라는 설계 지침으로 해석해야 한다.

## 3. 관련 연구가 제시하는 방법

### 3.1 검색 후 필요한 조각만 주입

RAG는 전체 지식 저장소를 프롬프트에 넣지 않고 query와 관련된 passage만 검색한다.
RETRO는 검색된 문서 chunk를 별도 encoder로 처리하고 chunked cross-attention으로
생성기에 연결했다.

이 저장소에서는 다음 자료를 외부 메모리로 볼 수 있다.

- `logs/runs/**/*.jsonl`의 실패·도구 결과
- `logs/receipts/**/*.json`의 측정값
- `docs/`의 버전별 해결책
- 현재 세션의 산출물 경로와 컴파일 오류

모델에는 전체 로그 대신 현재 실패 코드로 검색한 상위 2~4개 조각과 정확한 파일 경로만
전달한다. 모델 구조를 바꾸지는 않지만, 애플리케이션 계층에서 cross-attention과 같은
“외부 자료 중 필요한 것만 선택”하는 효과를 낸다.

### 3.2 입력을 작은 latent로 압축

Perceiver는 많은 입력이 작은 latent 배열에 cross-attention하고, 이후 계산은 이
latent에서 수행된다. 저장소에 그대로 적용하려면 qwen 자체를 바꿔야 하므로 단기
과제는 아니다. 대신 호스트가 다음과 같은 고정 스키마를 만들면 비슷한 병목을 흉내
낼 수 있다.

```json
{
  "goal": "A/D 이동과 Space 점프",
  "scene": "Assets/Scenes/...",
  "artifacts": ["Assets/Scripts/PlayerMovement.cs"],
  "failed_checks": ["player_did_not_jump"],
  "measurements": {"d_x": 4.95, "jump_y": 0.0},
  "next_action": "점프 입력 latch만 수정"
}
```

자연어 대화 전체보다 이 상태 객체를 다음 단계에 넘기는 편이 짧고, 경로·수치의 손실도
적다. 현재 `ArtifactLedger`와 verification repair prompt가 이미 이 방향이다.

### 3.3 Prompt compression

LLMLingua는 중요도가 낮은 토큰을 제거하는 방식으로 긴 프롬프트를 압축하며, 논문은
일부 평가에서 최대 20배 압축과 작은 성능 손실을 보고했다. LongLLMLingua는 긴 문서의
핵심 정보 위치와 순서를 고려한다.

이 프로젝트에는 주의가 필요하다. `unity_write_script`, `Assets/...`, 오류 코드처럼
한 글자도 바뀌면 안 되는 문자열이 많기 때문이다. 따라서 자유로운 대화·과거 설명만
압축하고 다음 항목은 보호해야 한다.

- 시스템 정책과 TaskContract 오류
- 도구명, JSON key, 파일 경로, 클래스명
- 최신 컴파일·런타임 오류 원문
- 호스트 측정값과 실패 코드

### 3.4 Sparse attention과 KV-cache 절약

Longformer와 BigBird는 모든 토큰 쌍을 보지 않는 sparse self-attention으로 긴 입력의
계산량을 선형에 가깝게 줄인다. 하지만 이는 모델 아키텍처·학습 단계의 변경이라 기존
qwen3-coder에 런타임 옵션 하나로 붙일 수 없다.

StreamingLLM과 H₂O는 오래된 KV를 전부 보존하지 않고 attention sink, 최근 토큰,
attention heavy hitter를 유지해 **KV-cache 메모리**를 줄인다. 이것은 긴 스트리밍
대화에 유용하지만, 프롬프트의 논리적 토큰 수나 잘못된 오래된 정보까지 자동으로
줄여주는 것은 아니다.

README의 `OLLAMA_KV_CACHE_TYPE=q8_0`도 같은 구분이 필요하다. 이는 KV 한 항목의
바이트 수를 줄여 VRAM을 아끼지만 `NUM_CTX=32768`의 토큰 개수를 줄이지는 않는다.

## 4. 현재 저장소와의 대응

현재 구현은 이미 다음 세 가지 콘텍스트 절약을 사용한다.

| 현재 기능 | attention 관점 |
|---|---|
| `_trim_history()`가 70% 예산에서 오래된 메시지 제거 | self-attention 입력 `n` 제한 |
| 플래너의 마일스톤별 fresh history | 작업마다 `n`을 초기화 |
| `ArtifactLedger`로 경로만 다음 마일스톤에 전달 | 원문을 작은 상태 latent로 압축 |
| 실패 코드·측정값만 fresh repair에 전달 | 관련 정보만 선택하는 retrieval과 유사 |

부족한 점은 `_trim_history()`가 오래된 메시지를 **관련도와 무관하게** 삭제한다는 것이다.
삭제된 메시지 중 중요한 경로나 사용자 결정을 복구할 수 없고, 반대로 최근의 반복
tool-call 결과는 계속 남을 수 있다.

## 5. 권장 설계

### 1단계 — 결정적 세션 상태

매 tool 결과를 대화에 계속 누적하는 대신 호스트가 다음 네 묶음으로 관리한다.

```text
고정: 시스템 정책, 원 요청, 현재 VerificationSpec
상태: 활성 씬, 작성 스크립트, 컴파일 상태, 마지막 측정값
최근: 마지막 2~4개 tool-call
검색: 현재 실패와 관련된 과거 로그·문서 top-k
```

완료된 성공 도구의 긴 JSON은 `path`, `status`, 핵심 수치만 남긴다. 원문은 JSONL에
보존하고 `/last`나 검색으로 필요할 때만 되가져온다.

### 2단계 — docs/log retrieval

BM25 같은 가벼운 로컬 검색부터 시작한다. embedding 모델을 추가하기 전에 실패 코드,
도구명, 파일명처럼 이 저장소에 풍부한 정확한 lexical key를 활용할 수 있다.

예:

```text
query = "player_did_not_jump spaceKey FixedUpdate"
result = 관련 HANDOFF 1개 + 버전 문서 1개 + 과거 실패/성공 로그 각 1개
```

각 조각은 500~1,000자 상한을 두고 출처 경로를 함께 전달한다.

### 3단계 — 선택적 prompt compression

검색으로도 큰 자유 서술에만 LLMLingua 계열 압축을 실험한다. exact identifier 보호 목록을
적용하고, 압축 전후로 경로·도구명·수치가 보존됐는지 호스트가 검사해야 한다.

## 6. 간단한 실험 계획

같은 Unity 요청 10회씩 다음 세 조건을 비교한다.

| 조건 | 내용 |
|---|---|
| A 기준선 | 현재 history trim |
| B 상태 압축 | 결정적 상태 + 최근 tool 4개 |
| C 상태+검색 | B + docs/log top-3 |

측정값:

- Ollama `prompt_eval_count`
- 첫 유효 tool-call까지 걸린 시간
- 잘못된 경로·도구 재호출 수
- `model_loop_completed`
- `build_stage_success`
- requested/measured/skipped checks

첫 목표는 B/C가 A 대비 prompt token을 **30% 이상 줄이면서**
`build_stage_success`를 떨어뜨리지 않는 것이다.

## 7. 연구 근거

- [Bahdanau et al., Neural Machine Translation by Jointly Learning to Align and Translate](https://arxiv.org/abs/1409.0473)
- [Vaswani et al., Attention Is All You Need](https://arxiv.org/abs/1706.03762)
- [Jaegle et al., Perceiver: General Perception with Iterative Attention](https://arxiv.org/abs/2103.03206)
- [Lewis et al., Retrieval-Augmented Generation](https://arxiv.org/abs/2005.11401)
- [Borgeaud et al., RETRO](https://arxiv.org/abs/2112.04426)
- [Jiang et al., LLMLingua](https://arxiv.org/abs/2310.05736)
- [Beltagy et al., Longformer](https://arxiv.org/abs/2004.05150)
- [Zaheer et al., BigBird](https://arxiv.org/abs/2007.14062)
- [Xiao et al., StreamingLLM](https://arxiv.org/abs/2309.17453)
- [Zhang et al., H₂O](https://arxiv.org/abs/2306.14048)

## 8. 한 줄 제안

현재 qwen3-coder를 바꾸기보다, **전체 history를 self-attention에 계속 넣지 말고
호스트가 상태를 압축한 뒤 관련 docs/log만 검색해 주입하는 구조**가 가장 싸고 안전하다.
