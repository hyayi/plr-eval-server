"""Eval-support layer (eval-server).

- scoring·report·gallery = 이 서버의 채점/렌더 코어.
- dataset·provenance = lab 과의 **공유 계약** vendored 복본(byte-identical —
  contract/CONTRACT.md, tests/test_contract_parity.py 로 드리프트 감지).

validate/plr_schema/vocab.yaml 은 vendoring 하지 않는다: 라벨 어휘 검증은
클라이언트 `lab validate-dataset` 소관이고 서버는 그것을 신뢰(SPEC:41),
push 시엔 구조 가드만 돈다."""
