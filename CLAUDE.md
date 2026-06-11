# CLAUDE.md — serial-mcp-server

@AGENTS.md

## Claude Code 전용

- 워크플로 3단계(병렬 실행)의 내장 수단은 **ultracode(Workflow)**다. 발동 기준은 AGENTS.md대로 — 상호 독립 작업이 여러 개 묶일 때만 쓰고, 보통 크기 기능은 단일 컨텍스트 직접 구현이 기본.
- 워크플로 4단계(검증)는 `/code-review`를 쓴다.
