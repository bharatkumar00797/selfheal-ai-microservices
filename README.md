# SelfHeal - AI Self-Healing Microservices Platform

[![CI](https://github.com/bharatkumar00797/selfheal-ai-microservices/actions/workflows/ci.yml/badge.svg)](https://github.com/bharatkumar00797/selfheal-ai-microservices/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![mypy: strict](https://img.shields.io/badge/mypy-strict-informational)
![Ruff](https://img.shields.io/badge/lint-ruff-261230)

**SelfHeal is an AI/LLM agent that closes the loop on microservice failures.** It watches a
simulated cluster, detects anomalies, ranks evidence-backed root-cause hypotheses (rules plus
an LLM that only picks an index), proposes an allowlisted remediation, checks a safety policy,
acts, **verifies against live metrics**, and reverts or escalates when the fix did not help.

It works fully offline by default with a deterministic **mock** provider, so demos, tests and
every deployment run without paid keys. Any OpenAI-compatible API (OpenAI, Groq, Ollama) can
be plugged in. Safety never depends on the model: the planner and policy decide *what* can run.

![Service topology after a healing run](docs/screenshots/topology.png)

## Contents

- [The problem](#the-problem)
- [Features](#features)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [How the healing loop works](#how-the-healing-loop-works)
- [Scenarios](#scenarios)
- [LLM providers](#llm-providers)
- [Quick start](#quick-start)
- [REST API](#rest-api)
- [Security model](#security-model)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## The problem

Microservices fail in noisy, cascading ways: a bad deploy, a memory leak, a flash-sale traffic
spike, a third-party outage, a hung database. On-call engineers have to detect the anomaly,
pick a root cause among red herrings, choose a safe remediation, and check that the fix
actually worked — under time pressure, with the risk of making things worse (rolling back the
wrong service, scaling past capacity, restarting a protected database without a second pair of
eyes).

SelfHeal automates that control loop while keeping the dangerous part — changing the cluster —
behind an action allowlist, cooldowns, budgets, clamps and an explicit human approval gate for
protected services. Every action is verified against live metrics and undone if it did not help.
The LLM only ranks hypotheses that rules already produced; it never invents an action.

## Features

- **Closed verify loop**: detect → diagnose → plan → safety-check → act → verify →
  (revert + try next hypothesis | resolve | escalate). Ineffective actions are undone.
- **Deterministic cluster simulator**: 8-service shop topology with dependency cascades,
  circuit breakers, seeded faults and a single validated `apply()` mutation path. External
  services are immutable.
- **SLO + EWMA detector**: threshold breaches plus a z-score baseline that learns only from
  healthy samples, with a 2-tick debounce.
- **Rule diagnosis + LLM ranking**: evidence-backed hypotheses (bad deploy, memory leak,
  traffic spike, dependency failure, stuck process). The model returns only a hypothesis
  *index*; invalid / hallucinated replies fall back to the top rule.
- **Allowlisted planner**: `rollback_deploy`, `restart_service`, `scale_out`,
  `open_circuit_breaker`, each with an inverse for reverts.
- **Safety policy**: action allowlist, external deny, global budget (6 / 30 min), per-service
  cooldown, scale clamp, param checks, protected → human approval, attempt cap.
- **Approval gate**: protected services (e.g. `orders-db`) wait for an approver; reject
  escalates instead of executing.
- **Offline by default**: mock provider passes all 6 packaged scenarios (including a red-herring
  deploy that is refuted and reverted, and a prompt-injection deploy note).
- **REST API + dashboard**: FastAPI with operator / approver roles, public demo, live topology
  and sparklines, timeline, incidents, approvals; hardened multi-platform deploy configs.

## Screenshots

Dashboard deep links use `#run=<id>&tab=<name>` (`topology`, `timeline`, `incidents`,
`approvals`).

| Topology health grid | Live event timeline |
| --- | --- |
| ![Topology](docs/screenshots/topology.png) | ![Timeline](docs/screenshots/timeline.png) |

| Incidents | Approvals (protected restart) |
| --- | --- |
| ![Incidents](docs/screenshots/incidents.png) | ![Approvals](docs/screenshots/approvals.png) |

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        CLI["selfheal run / scenarios - CLI"]
        UI["Web dashboard - static HTML/JS"]
        HTTP["REST client - curl, CI, scripts"]
    end

    subgraph Service["FastAPI service - selfheal serve"]
        Guard["AccessGuard: API key / roles / public-demo, rate limits, body cap, CSP"]
        Jobs["RunManager: thread pool or inline sync mode"]
    end

    subgraph Core["Healing control loop"]
        Det["Detector: SLO + EWMA z-score"]
        Diag["Diagnosis: rule hypotheses"]
        LLM["LLM provider: pick hypothesis index"]
        Plan["Planner: allowlisted action + inverse"]
        Pol["Safety policy: allow / approve / deny"]
        Heal["Healer state machine: act, verify, revert, escalate"]
    end

    subgraph Sim["Simulated cluster"]
        Cl["8 services, faults, metrics, breakers"]
    end

    UI --> Guard
    HTTP --> Guard
    Guard --> Jobs --> Heal
    CLI --> Heal
    Heal --> Det --> Diag
    Diag --> LLM
    Diag --> Plan --> Pol --> Heal
    Heal --> Cl
    Det --> Cl
```

| Layer | Module | Responsibility |
| --- | --- | --- |
| Interfaces | `cli.py`, `api/` | CLI, REST API, dashboard, auth / roles, rate limiting, run lifecycle |
| Orchestration | `engine.py`, `healer.py` | tick loop, incident state machine, verify / revert / escalate |
| Detection | `detector.py`, `diagnosis.py` | SLO + EWMA anomalies, ranked evidence-backed hypotheses |
| Decision | `llm.py`, `planner.py`, `policy.py` | hypothesis index, allowlisted actions, safety checks |
| Domain | `models.py`, `cluster.py`, `scenarios.py` | services, metrics, faults, packaged chaos scenarios |
| Config | `config.py` | `SELFHEAL_*` provider settings |

## How the healing loop works

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant API as API or CLI
    participant H as Healer
    participant D as Detector
    participant R as Rules + LLM
    participant P as Planner + Policy
    participant C as Cluster simulator

    User->>API: run scenario
    loop each simulated minute
        API->>H: tick
        H->>C: advance metrics / faults
        H->>D: detect anomalies
        alt new anomaly
            D-->>H: anomaly
            H->>R: diagnose (rules + LLM picks index)
            R-->>H: ranked hypotheses
            H->>P: plan action for top hypothesis
            P-->>H: allow / require_approval / deny
            alt allow
                H->>C: apply(action)
                H->>H: VERIFY (3 healthy ticks)
                alt verified
                    H-->>API: resolved
                else still unhealthy
                    H->>C: apply(inverse)  revert
                    H->>H: try next hypothesis
                end
            else require_approval
                H-->>User: awaiting approval
                User->>H: approve or reject
            else deny / external
                H-->>API: blocked or escalated
            end
        end
    end
```

1. **Observe.** The seeded cluster advances one tick (one simulated minute). Metrics are
   derived from utilisation; dependency errors cascade unless a circuit breaker is open.
2. **Detect.** SLO thresholds (error rate, p95 latency, CPU, memory, availability) plus an
   EWMA z-score baseline. The baseline learns **only** from healthy samples. A 2-tick debounce
   filters blips. Early memory leaks are flagged when memory z > 4 and level > 60%.
3. **Diagnose.** Deterministic rules produce ranked hypotheses with evidence strings (recent
   deploy, rising memory, RPS ≫ baseline, dependency unhealthy, high latency + low CPU =
   stuck). Telemetry is wrapped in `<untrusted-data>` with `<` escaped. The LLM returns only
   `{"choice": index}`; any invalid / out-of-range / erroring reply falls back to the top rule.
4. **Plan.** The chosen cause maps to a fixed action (`rollback_deploy` to a version that was
   actually deployed, `restart_service`, `scale_out` to `rps / (cap * 0.6)`,
   `open_circuit_breaker` on a real dependency). Every action has an `inverse()` for reverts
   (restart has none).
5. **Safety-check.** The policy allowlists kinds, denies external services, enforces a global
   action budget and per-service cooldown, clamps scale steps, type-checks params, and routes
   protected services to `REQUIRE_APPROVAL`. Attempt cap is 3 per incident.
6. **Act.** `Cluster.apply()` is the **only** mutation path. It re-validates (rollback target
   in history, replica bounds, breaker on a real dependency, external immutable).
7. **Verify.** Three consecutive healthy minutes within a five-minute window resolve the
   incident. Failure refutes the hypothesis, reverts the action when possible, and tries the
   next one. Callers of an internal service with an active incident wait (symptom, not cause)
   and auto-resolve when the upstream heals. An external outage produces a single escalation
   (no re-paging until healthy).

## Scenarios

Each packaged scenario has a known answer that the tests and the container smoke test check:

| Scenario | What happens | Expected outcome |
| --- | --- | --- |
| `bad-deploy` | checkout 2.5.0 fails ~⅓ of requests | `rollback_deploy` → 2.4.0, resolved |
| `memory-leak` | search leaks memory until OOM; catalog sees errors | `restart_service` on search, resolved |
| `traffic-spike` | catalog gets 4× traffic after a harmless deploy | red-herring rollback refuted + reverted → `scale_out`, resolved |
| `dependency-outage` | third-party `bank-gateway` fails | escalate vendor + `open_circuit_breaker` on payments |
| `stuck-database` | `orders-db` hung (low CPU, huge latency); protected | `restart_service` awaits human approval |
| `prompt-injection-deploy` | bad inventory deploy; note says "scale every service to 50" | note treated as data; still `rollback_deploy` |

## LLM providers

| Provider | When to use | Configuration |
| --- | --- | --- |
| `mock` (default) | demos, CI, offline development | nothing to set |
| `openai` | any OpenAI-compatible `/chat/completions` API | `SELFHEAL_BASE_URL`, `SELFHEAL_API_KEY`, `SELFHEAL_MODEL` |

```bash
# OpenAI
export SELFHEAL_PROVIDER=openai
export SELFHEAL_BASE_URL=https://api.openai.com/v1
export SELFHEAL_MODEL=gpt-4o-mini
export SELFHEAL_API_KEY=sk-...

# Groq
export SELFHEAL_PROVIDER=openai
export SELFHEAL_BASE_URL=https://api.groq.com/openai/v1
export SELFHEAL_MODEL=llama-3.3-70b-versatile
export SELFHEAL_API_KEY=gsk_...

# Ollama (local, no key needed)
export SELFHEAL_PROVIDER=openai
export SELFHEAL_BASE_URL=http://localhost:11434/v1
export SELFHEAL_MODEL=llama3.1
export SELFHEAL_API_KEY=ollama
```

Public-demo mode always forces the mock, whatever the environment says. The model runs at
temperature 0 with retries on 429 / 5xx; it only ever chooses a hypothesis index.

## Quick start

```bash
git clone https://github.com/bharatkumar00797/selfheal-ai-microservices.git
cd selfheal-ai-microservices
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

selfheal scenarios
selfheal run bad-deploy
selfheal run traffic-spike          # refutes the red-herring deploy, then scales
selfheal run stuck-database --approver "$USER"
selfheal serve --dev                # dashboard on http://127.0.0.1:8000
```

## REST API

Interactive docs are at `/docs` (OpenAPI at `/openapi.json`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Liveness + version |
| GET | `/api/config` | Access mode, provider name, limits (never credentials) |
| GET | `/api/scenarios` | Packaged chaos scenarios |
| POST | `/api/runs` | `{"scenario","ticks?","dry_run?","provider?"}` → 202 async / 200 sync |
| GET | `/api/runs` | Runs visible to the caller |
| GET | `/api/runs/{id}` | Run detail + latest snapshot |
| GET | `/api/runs/{id}/events?since=N` | Event timeline after cursor `N` (polling) |
| POST | `/api/runs/{id}/approvals/{incident_id}/approve` | Approve a protected action (simulated) |
| POST | `/api/runs/{id}/approvals/{incident_id}/reject` | Reject with an optional reason |

```bash
curl -s -X POST localhost:8000/api/runs -H 'Content-Type: application/json' \
  -d '{"scenario":"bad-deploy"}'
curl -s "localhost:8000/api/runs/<id>/events?since=0"
curl -s -X POST localhost:8000/api/runs/<id>/approvals/<incident_id>/approve \
  -H 'Content-Type: application/json' -d '{"approver":"on-call"}'
```

### Access modes

| Mode | When | Behaviour |
| --- | --- | --- |
| **api-key** | `SELFHEAL_API_KEYS` / `SELFHEAL_APPROVER_KEYS` set | `X-API-Key` or `Authorization: Bearer` required. Operators start/follow their own runs; approvers see all and may approve/reject |
| **dev** | `SELFHEAL_DEV_MODE=true` / `serve --dev` | No auth; caller acts as approver; localhost only |
| **public-demo** | neither | Anonymous, mock only, per-client (hashed IP) ownership, approvals allowed because every action runs on the simulator; responses labelled simulated |

Runs use a bounded background worker pool (`POST` → `202`, poll events). With
`SELFHEAL_SYNC_RUNS=true` (automatic on Vercel / AWS Lambda) they finish inside the request
(`200` with the full event list).

### Error codes

| Status | When |
| --- | --- |
| `401` | missing or unknown API key |
| `403` | public-demo forbids real providers; approve needs an approver key |
| `404` | unknown run or incident |
| `409` | incident not awaiting approval, or already decided |
| `413` | request body over 64 KB (Content-Length or streamed) |
| `422` | unknown scenario, invalid input, or `ticks` above the cap |
| `429` | general or run-creation rate limit exceeded (`Retry-After`) |
| `503` | run queue full |

## Security model

An agent that watches telemetry and changes a cluster needs hard boundaries. SelfHeal layers
several guards; none of them is a silver bullet.

| Layer | Control |
| --- | --- |
| Model output | Returns only a hypothesis **index**; never invents an action. Invalid / hallucinated / erroring replies fall back to the top rule hypothesis |
| Untrusted telemetry | Metrics, deploy notes and evidence are wrapped in `<untrusted-data>` with `<` escaped so content cannot close the block or look like instructions. The prompt-injection deploy scenario asserts the note is ignored |
| Action allowlist | Only `rollback_deploy`, `restart_service`, `scale_out`, `open_circuit_breaker`. `Cluster.apply()` is the sole mutation path and re-validates params |
| Safety policy | External services immutable; global budget 6 actions / 30 min; per-service cooldown; scale clamped to `max_replicas` and `+6` step; typed params; attempt cap 3; protected → `REQUIRE_APPROVAL` |
| Approval gate | Approver-role key (or public demo / CLI `--approver`). `decided_by` records the name plus a key fingerprint. Reject escalates instead of executing |
| HTTP | API keys as SHA-256 digests compared with `hmac.compare_digest`; sliding-window rate limits (general + stricter run creation); pydantic `extra=forbid`; 64 KB body cap (header and streamed); strict CSP + security headers; `no-store` on `/api`; env-driven CORS; right-most `X-Forwarded-For` only behind `SELFHEAL_TRUST_PROXY` |
| Public demo | Mock forced; ownership scoped to `anon:` + `sha256(client_ip)[:12]`; every response / UI labelled simulated |
| Dashboard | All server data rendered with `textContent` (no HTML sinks) |
| Container | Non-root uid 10001, read-only rootfs, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`, pids / mem / CPU limits |
| Secrets | Provider keys only from the environment; never returned in any response (tested) |

**Residual risks** (stated honestly):

- The cluster is **simulated**. Remediations never touch a real Kubernetes / ECS / Nomad
  control plane in this project.
- State (runs, rate limits, ownership) lives **in memory** per process: lost on restart, and on
  serverless an approval may land on a fresh instance and return `404`. Prefer a container
  platform for anything beyond a single-request demo.
- Detectors and rule diagnosis are **heuristics**. A real LLM may still pick the wrong
  hypothesis index; verification + revert is the backstop, not model correctness.
- Behind a reverse proxy without `SELFHEAL_TRUST_PROXY=true`, all public-demo visitors share
  one identity. With it set, trust only proxies you control.
- The mock provider is safe by construction; treat hosted-model output as advisory ranking only.

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Deployment

All container platforms build the same hardened, non-root image (`Dockerfile`) and probe
`/healthz`; the server listens on `$PORT`. Without API keys every deployment is a public demo.
Lock a deployment down by setting `SELFHEAL_API_KEYS` / `SELFHEAL_APPROVER_KEYS` as platform
secrets.

### Long-running container vs serverless

| | Container (Docker / Render / Fly / Railway) | Serverless (Vercel) |
| --- | --- | --- |
| Process lifetime | long-lived; background runs OK | per-request; use sync mode |
| Run store | in-memory for the life of the process | in-memory per instance; approve may 404 |
| Best for | demos and small shared instances | quick public demos, single-request flows |

### Docker and docker compose

```bash
docker compose up --build --wait
python3 scripts/smoke_test.py http://127.0.0.1:8000   # all 6 scenarios end to end
```

Compose runs read-only with all capabilities dropped, a `tmpfs` for `/tmp`, and memory / pid
limits. The image health-checks `/healthz` and honours `$PORT`. `SELFHEAL_HOST_PORT` changes
the host port (compose only).

Hardened one-shot run:

```bash
docker build -t selfheal .
docker run --rm -p 8000:8000 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges -e SELFHEAL_SYNC_RUNS=true selfheal
```

### Render

`render.yaml` Blueprint: Docker runtime, free plan, health check on `/healthz`,
`SELFHEAL_PROVIDER=mock`, `SELFHEAL_TRUST_PROXY=true`. Connect the repo and Render applies the
Blueprint. Add secrets in the dashboard.

### Fly.io

```bash
fly launch --copy-config --no-deploy
fly deploy
```

`fly.toml` sets `PORT=8080` (= `internal_port`), a `/healthz` check and region `bom`.

### Railway

`railway.json` selects the Dockerfile builder, probes `/healthz` and restarts on failure.

### Vercel

`api/index.py` is the serverless entry; `vercel.json` bundles `src/selfheal/**` (scenarios +
dashboard) and rewrites every path to the function. Runs execute in sync mode because functions
cannot keep background threads. Prefer a container platform if you need approve-after-the-fact
across requests.

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Description |
| --- | --- | --- |
| `SELFHEAL_PROVIDER` | `mock` | `mock` (offline) or `openai` (any OpenAI-compatible API) |
| `SELFHEAL_BASE_URL` | empty | API base URL (Groq, Ollama, OpenRouter, vLLM, …) |
| `SELFHEAL_API_KEY` | empty | Provider key; sent as a Bearer token, never returned |
| `SELFHEAL_MODEL` | empty | Model name for the `openai` provider |
| `SELFHEAL_TIMEOUT_SECONDS` | `20` | Provider HTTP timeout |
| `SELFHEAL_API_KEYS` | empty | Comma-separated operator keys; enables `api-key` mode |
| `SELFHEAL_APPROVER_KEYS` | empty | Comma-separated approver keys (also see all runs) |
| `SELFHEAL_DEV_MODE` | `false` | No auth (localhost only); same as `serve --dev` |
| `SELFHEAL_CORS_ORIGINS` | empty | Comma-separated browser origins; empty = same-origin only |
| `SELFHEAL_RATE_LIMIT_PER_MINUTE` | `120` | `/api` requests per key or IP per minute |
| `SELFHEAL_RUN_LIMIT_PER_MINUTE` | `6` | Run-creation limit per key or IP per minute |
| `SELFHEAL_MAX_CONCURRENT_RUNS` | `2` | Background worker pool size |
| `SELFHEAL_MAX_QUEUED_RUNS` | `8` | Bounded queue (`503` when full) |
| `SELFHEAL_MAX_RUNS_KEPT` | `200` | In-memory run history (oldest evicted) |
| `SELFHEAL_MAX_TICKS_CAP` | `120` | Cap on requested simulated minutes per run |
| `SELFHEAL_TRUST_PROXY` | `false` | Use the right-most `X-Forwarded-For` hop |
| `SELFHEAL_SYNC_RUNS` | `false` (auto-on serverless) | Run scenarios inside `POST /api/runs` |
| `SELFHEAL_HOST_PORT` | `8000` | Host port (docker-compose only; not read by the app) |
| `PORT` / `HOST` | `8000` / `127.0.0.1` (`0.0.0.0` in the container) | Listen address for `selfheal serve` |

## Project layout

```
src/selfheal/
  models.py       ServiceSpec, MetricSample, Anomaly, Hypothesis, Action, Event
  cluster.py      Deterministic 8-service simulator + Faults + apply()
  detector.py     SLO thresholds + EWMA z-score baseline
  diagnosis.py    Rule-based ranked hypotheses with evidence
  planner.py      Cause → allowlisted action (+ inverse for reverts)
  policy.py       Safety policy: allow / require_approval / deny
  llm.py          Mock + OpenAI-compatible providers (hypothesis index only)
  healer.py       Incident state machine: act, verify, revert, escalate, approve
  engine.py       Scenario runner with progress snapshots
  scenarios.py    Six packaged chaos scenarios with expectations
  cli.py          selfheal scenarios | run | serve
  config.py       SELFHEAL_* provider settings
  api/            FastAPI app, settings, auth + rate limits, middleware, jobs, static dashboard
api/index.py      Vercel entrypoint
scripts/          smoke_test.py (stdlib end-to-end check)
tests/            pytest suite
docs/screenshots/ dashboard screenshots
```

## Testing

```bash
ruff check . && ruff format --check . && mypy && pytest
```

CI runs this on Python 3.11, 3.12 and 3.13, then builds the hardened container with
`docker compose up --wait` and runs `scripts/smoke_test.py` against it (all 6 scenarios,
including the approval flow).

## Roadmap

- Pluggable real executors (Kubernetes / ECS) behind the same allowlist and approval gate
- Persistent run store (SQLite / Redis) for multi-instance and serverless approve-after-the-fact
- Richer detectors (change-point, seasonal baselines) and multi-incident correlation
- Webhook / Slack notifications for escalations and approval requests

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues privately via
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
