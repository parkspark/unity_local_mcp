# Self-Attention으로 토큰·콘텍스트를 줄이는 방법

작성일: 2026-07-29  
대상 모델: `qwen3-coder:30b` (Ollama, 로컬)  
목적: Unity 에이전트의 긴 tool-call 히스토리를 줄일 수 있는 self-attention 기반 방법 정리

## 1. 요약

**Self-attention은 콘텍스트를 줄이는 기능이 아니라, 주어진 콘텍스트에서 무엇이 중요한지
계산하는 기능이다.** 따라서 self-attention을 사용한다는 이유만으로 입력 토큰이
자동으로 줄지는 않는다.

토큰이나 메모리를 실제로 줄이려면 attention 결과를 이용해 다음 중 하나를 해야 한다.

1. 중요하지 않은 입력 토큰을 다음 호출에서 제외한다.
2. 중요도가 낮은 과거 KV cache를 제거한다.
3. 모든 토큰 쌍을 계산하지 않는 sparse attention을 사용한다.
4. 긴 기록을 작은 상태 표현으로 압축한 뒤 필요한 원문만 다시 가져온다.

현재 `qwen3-coder:30b`를 Ollama로 실행하는 이 프로젝트에는 모델 내부 attention
weight를 직접 제어하는 기능이 없다. 그러므로 가장 현실적인 방법은 **호스트가
self-attention이 중요하게 볼 만한 정보의 조건을 명시적으로 흉내 내는 것**이다.

```text
전체 대화 이월
    ↓
정책 + 현재 상태 + 최근 도구 결과 + 관련 과거 기록만 선택
    ↓
qwen3-coder self-attention
```

## 2. Self-Attention 개념

Self-attention은 같은 토큰열에서 query, key, value를 만든다.

```text
Q = XWq
K = XWk
V = XWv

SelfAttention(X) = softmax(QKᵀ / √d) V
```

각 토큰은 다른 토큰과의 관련도를 계산해 다음 표현을 만든다. 예를 들어 현재 모델이
`player_did_not_jump`를 처리할 때 다음 토큰에 높은 비중을 줄 가능성이 있다.

```text
spaceKey
jumpRequested
FixedUpdate
isGrounded
PlayerMovement.cs
```

반대로 이미 끝난 씬 생성 설명이나 오래된 성공 응답은 현재 점프 실패와 관련도가 낮다.
문제는 **낮은 비중을 받는 토큰도 프롬프트에 들어온 순간 콘텍스트 공간과 KV cache를
차지한다는 것**이다.

Self-attention이 중요도를 계산하는 것과 입력 토큰을 실제로 삭제하는 것은 별개의
단계다.

## 3. 토큰이 길어질 때 발생하는 비용

표준 full self-attention은 길이 `n`인 입력에서 토큰 쌍을 비교하므로 attention 계산이
대략 `O(n²)`로 증가한다. 생성 중 보존하는 KV cache는 레이어 수와 토큰 수에 비례해
대략 선형으로 증가한다.

이 프로젝트는 `NUM_CTX=32768`이고, history가 설정 예산을 넘으면 오래된 메시지부터
삭제한다. 기존
[한국어 vs 영어 명령 문서](korean_vs_english_prompting.md)의 측정에서는 같은 의미의
한국어 문장이 영어보다 약 1.6배 많은 토큰을 사용했다.

따라서 다음 두 문제를 구분해야 한다.

| 문제 | 줄여야 하는 것 | 대표 방법 |
|---|---|---|
| 프롬프트가 너무 김 | 입력 토큰 수 | 검색, 요약, token pruning |
| KV cache가 VRAM을 많이 씀 | 토큰별 K/V 저장 공간 | 양자화, eviction, sliding window |
| attention 계산이 비쌈 | 토큰 간 연결 수 | sparse/local attention |

`OLLAMA_KV_CACHE_TYPE=q8_0`은 두 번째 문제를 완화하지만 첫 번째 문제의 토큰 수는
줄이지 않는다.

## 4. Self-Attention을 활용한 절약 방법

### 4.1 Attention 기반 token pruning

일부 토큰이 여러 레이어와 head에서 계속 낮은 attention을 받는다면 이후 계산이나
다음 요청에서 제거할 수 있다.

```text
원본 10,000 tokens
    ↓ attention 중요도 측정
정책·경로·오류와 관련된 3,000 tokens 선택
    ↓
다음 모델 호출
```

장점:

- 의미 없는 반복 설명과 성공 로그를 직접 제거할 수 있다.
- 현재 질문과 관련된 토큰을 더 높은 밀도로 배치할 수 있다.

위험:

- 낮은 attention이 낮은 중요성을 항상 뜻하지는 않는다.
- 한 번만 등장한 파일 경로나 숫자는 attention score가 낮아도 반드시 보존해야 한다.
- Ollama API는 qwen3-coder의 head별 attention weight를 현재 직접 반환하지 않는다.

따라서 이 프로젝트에서 순수 attention score pruning을 하려면 모델 런타임 수정이
필요하다.

### 4.2 Attention heavy hitter 기반 KV 제거

H₂O 연구는 과거 토큰 중 소수의 **attention heavy hitter**가 큰 비중을 차지한다는
관찰을 이용한다. 최근 토큰과 누적 attention이 높은 토큰은 남기고, 중요도가 낮은
토큰의 KV를 제거한다.

에이전트 문맥으로 바꾸면 다음과 같다.

```text
항상 유지:
- 시스템 정책
- 원 요청
- 현재 씬과 파일 경로
- 최근 실패 코드
- 반복해서 참조된 측정값

제거 후보:
- 오래된 자연어 설명
- 성공한 unity_ping 전체 JSON
- 동일한 unity_wait 결과 반복
- 이미 고쳐진 컴파일 오류
```

이 방식은 KV 메모리에는 효과적이지만, 제거된 토큰을 모델이 다시 정확히 복원할 수
없다는 위험이 있다. 원문은 실행 JSONL에 계속 보존해야 한다.

### 4.3 Attention sink + sliding window

StreamingLLM은 단순히 가장 오래된 KV부터 버리면 품질이 급격히 떨어질 수 있으며,
초기 토큰 일부를 attention sink로 유지하면 sliding window가 안정된다는 결과를
제시했다.

현재 에이전트에 대응시키면 다음 구조다.

```text
[초기 고정 영역]
시스템 프롬프트 + 원 요청 + 핵심 정책

[압축 상태 영역]
현재 씬 + 산출물 + 실패 코드

[최근 window]
마지막 N개 tool-call과 결과
```

모든 과거 대화를 유지하지 않고도 초기 지시와 최근 작업 흐름을 함께 보존한다.
현재 `_trim_history()`는 시스템 메시지는 남기지만 오래된 메시지를 단순 FIFO로
삭제한다. 이를 세 영역 구조로 바꾸면 중요 정보의 우연한 삭제를 줄일 수 있다.

### 4.4 Sparse self-attention

Longformer와 BigBird는 모든 토큰이 모든 토큰을 보는 대신 다음 연결을 조합한다.

- 주변 토큰을 보는 local attention
- 일부 전역 토큰을 보는 global attention
- 제한된 sparse 또는 random attention

이론적으로 full attention의 `O(n²)` 비용을 선형에 가깝게 줄일 수 있다.

Unity 에이전트에 대응하면 `VerificationSpec`, 실패 코드, 현재 씬 경로를 global
token처럼 취급하고, 개별 tool-call은 주변 결과만 보게 하는 구조다.

하지만 sparse attention은 **모델 아키텍처와 학습 방식의 변경**이다. 이미 학습된
qwen3-coder를 Ollama 옵션만 바꿔 Longformer처럼 만들 수는 없다.

### 4.5 Attention-guided prompt compression

LLMLingua 계열은 작은 언어 모델의 중요도 또는 perplexity를 이용해 긴 프롬프트에서
덜 중요한 토큰을 제거한다. 엄밀히 말하면 qwen 본체의 self-attention weight를 직접
사용하는 방법은 아니지만, “모델이 다음 추론에 중요하게 사용할 토큰을 먼저 고른다”는
점에서 실용적인 attention-guided compression에 가깝다.

이 프로젝트에는 보호 목록이 필요하다.

```text
절대 압축하지 않음:
- unity_* 도구명
- Assets/... 경로
- C# 클래스·필드명
- JSON key
- 오류 코드와 측정값

압축 가능:
- 반복된 자연어 설명
- 오래된 계획
- 성공한 도구의 장문 부가 정보
- 이미 해결된 실패의 상세 stack trace
```

## 5. 현재 프로젝트에 가장 현실적인 방법

모델 내부 attention을 읽지 못하므로, 호스트가 **attention proxy score**를 계산하는
방식이 현실적이다.

### 5.1 보존 점수

각 메시지와 tool 결과에 다음 점수를 부여한다.

| 조건 | 점수 예시 |
|---|---:|
| 시스템 정책·원 요청 | 무조건 보존 |
| 현재 실패 코드 포함 | +10 |
| 현재 씬·스크립트 경로 포함 | +8 |
| 최근 4개 tool-call | +6 |
| VerificationSpec 항목과 일치 | +5 |
| 성공한 반복 wait/ping | -5 |
| 이미 해결된 오류 | -4 |
| 같은 결과의 중복 | -6 |

attention weight 대신 실패 코드·도구명·경로의 정확한 일치를 사용한다. 이 저장소는
자유 대화보다 구조화된 식별자가 많아 단순 lexical score도 유용할 가능성이 높다.

### 5.2 권장 콘텍스트 구조

```text
1. System prompt
2. 원래 사용자 요청
3. 현재 TaskContract / VerificationSpec
4. 압축된 작업 상태
5. 현재 실패와 관련된 docs/log top-k
6. 최근 tool-call 4개
```

압축 상태 예:

```json
{
  "active_scene": "Assets/Scenes/Game.unity",
  "scripts": ["Assets/Scripts/PlayerMovement.cs"],
  "compile_errors": [],
  "failed_checks": ["player_did_not_jump"],
  "last_measurement": {"jump_y": 0.0},
  "next_required": ["fix jump latch", "host reverify"]
}
```

긴 tool 결과 원문은 `logs/runs/**/*.jsonl`에 남겨 두고, 모델이 필요로 할 때만 검색해
다시 주입한다.

## 6. 기존 설계와의 대응

현재 저장소에는 이미 self-attention 입력을 줄이는 기반이 있다.

| 기존 기능 | 효과 |
|---|---|
| `_trim_history()` | 전체 history 토큰 상한 |
| 마일스톤별 fresh history | 이전 마일스톤 토큰 제거 |
| `ArtifactLedger` | 전체 대화 대신 산출물 경로 전달 |
| fresh repair context | 성공 과정 대신 실패 항목만 전달 |
| tool 결과 절단 | 단일 결과의 과도한 토큰 방지 |

다음 개선은 단순 삭제를 **관련도 기반 선택**으로 바꾸는 것이다.

```text
현재: 오래된 순서대로 삭제
개선: 고정 정보 보존 + 현재 실패 관련도 + 최근 window
```

## 7. 간단한 측정 계획

아래 세 조건으로 동일한 Unity 요청을 반복 실행한다.

| 조건 | history 구성 |
|---|---|
| A | 현재 FIFO trim |
| B | 고정 영역 + 압축 상태 + 최근 4개 tool-call |
| C | B + 현재 실패 관련 docs/log top-3 |

측정 항목:

- Ollama `prompt_eval_count`
- 최대 history 추정 토큰
- tool-call 반복 횟수
- 잘못된 경로·클래스명 발생 수
- 첫 검증까지 걸린 시간
- `model_loop_completed`
- `build_stage_success`
- requested/measured/skipped checks

초기 성공 기준:

```text
prompt_eval_count 30% 이상 감소
build_stage_success 하락 없음
경로·도구명 오류 증가 없음
measured_checks 감소 없음
```

이 수치는 아직 실측 결과가 아니라 **후속 실험의 목표값**이다.

## 8. 확실하지 않은 것

1. qwen3-coder의 실제 attention head가 Unity 도구명·경로·오류 코드에 어떤 비중을
   주는지는 측정하지 않았다.
2. attention score가 낮은 토큰을 제거해도 tool-call 정확도가 유지된다는 보장은 없다.
3. H₂O·StreamingLLM의 결과가 Ollama의 현재 qwen3-coder 런타임에 그대로 적용된다는
   보장은 없다.
4. prompt compression은 자연어 QA보다 정확한 코드·JSON 복사 작업에서 더 큰 손실을
   낼 수 있다.
5. sparse attention은 모델을 다시 학습하거나 최소한 런타임을 수정해야 하므로
   단기 구현 범위가 아니다.

따라서 첫 구현은 attention weight를 직접 사용하지 않고, **호스트가 이미 신뢰하는
실패 코드·경로·검증 명세로 중요도를 결정하는 방식**이어야 한다.

## 9. 결론 및 후속

Self-attention은 “무엇을 볼지” 정하지만 “무엇을 입력하지 않을지”는 정하지 않는다.
콘텍스트를 아끼려면 attention의 중요도 개념을 입력 전 선택 단계에 적용해야 한다.

현재 프로젝트의 권장 순서는 다음과 같다.

1. 전체 history 대신 결정적 상태 객체를 만든다.
2. 시스템 정책과 원 요청은 초기 고정 영역으로 유지한다.
3. 최근 tool-call은 작은 sliding window로 유지한다.
4. 현재 실패와 관련된 docs/log만 검색한다.
5. exact identifier를 보호한 뒤 자연어 부분에만 prompt compression을 실험한다.
6. 효과는 `prompt_eval_count`와 실제 검증 영수증으로 판정한다.

**한 줄 결론:** qwen3-coder의 self-attention을 바꾸기보다, self-attention에 들어가기
전 호스트가 중요한 토큰만 선택하는 편이 현재 구조에서 가장 안전하다.

## 10. 참고 연구

- [Vaswani et al., Attention Is All You Need](https://arxiv.org/abs/1706.03762)
- [Beltagy et al., Longformer](https://arxiv.org/abs/2004.05150)
- [Zaheer et al., BigBird](https://arxiv.org/abs/2007.14062)
- [Xiao et al., StreamingLLM](https://arxiv.org/abs/2309.17453)
- [Zhang et al., H₂O](https://arxiv.org/abs/2306.14048)
- [Jiang et al., LLMLingua](https://arxiv.org/abs/2310.05736)
