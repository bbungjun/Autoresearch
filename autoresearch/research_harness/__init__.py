"""재현 가능한 평가 snapshot 패키지.

[파이프라인] action log 일일 파티션과 P0-2 Sealed Judge 사이에서 평가용
label-free slate와 Judge 전용 label artifact를 조립하는 경계를 담당한다.

[기능] Stage B 내부 typed contract와 snapshot builder 구성 요소를 제공한다.

[비책임] action log 생성(autoresearch.action_log_generation), 후보 학습·실행과
metric/Judge 판정(P0-2 이후)을 담당하지 않는다.
"""
