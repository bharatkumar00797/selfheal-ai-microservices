# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-12

First stable release.

### Added

- Closed self-healing control loop: detect → diagnose → plan → safety-check → act → verify →
  (revert + try next hypothesis | resolve | escalate).
- Deterministic 8-service cluster simulator with dependency cascades, circuit breakers, seeded
  faults (bad deploy, memory leak, traffic spike, outage, stuck) and a single validated
  `apply()` mutation path; external services are immutable.
- SLO + EWMA z-score anomaly detector (baseline learns only from healthy samples, 2-tick debounce).
- Rule-based diagnosis producing ranked evidence-backed hypotheses; pluggable LLM providers
  (offline mock + OpenAI-compatible OpenAI / Groq / Ollama) that only pick a hypothesis index,
  with telemetry wrapped as `<untrusted-data>` and safe fallback on invalid replies.
- Allowlisted planner (`rollback_deploy`, `restart_service`, `scale_out`, `open_circuit_breaker`)
  with inverses for reverts.
- Safety policy: action allowlist, external deny, global action budget, per-service cooldown,
  scale clamp, param checks, protected-service approval gate, attempt cap.
- Healer state machine with verification (3 healthy minutes), refute + revert, symptom-service
  waiting, single escalation for external outages, dry-run and approve / reject.
- Six packaged scenarios: bad-deploy, memory-leak, traffic-spike (red herring refuted),
  dependency-outage, stuck-database (approval), prompt-injection-deploy.
- CLI: `selfheal scenarios`, `selfheal run`, `selfheal serve`.
- FastAPI service with run / events / approvals endpoints, OpenAPI docs, operator / approver
  API-key roles, dev and public-demo access modes, rate limits, 64 KB body cap, strict CSP and
  security headers.
- No-build dashboard: topology health grid with SVG sparklines, live event timeline, incidents,
  approvals inbox, `#run=&tab=` deep links.
- Hardened multi-stage Docker image, docker compose, Render, Fly.io, Railway and Vercel configs,
  end-to-end smoke test script and Docker smoke job in CI.

[Unreleased]: https://github.com/bharatkumar00797/selfheal-ai-microservices/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/bharatkumar00797/selfheal-ai-microservices/releases/tag/v1.0.0
