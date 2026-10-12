# SelfHeal - AI Self-Healing Microservices Platform

An AI-assisted control loop for microservices: it watches service health signals, detects
anomalies, diagnoses the likely root cause (rules + an LLM), and applies **guarded, verified**
remediations such as restart, scale-out, rollback or circuit-breaking - and rolls back or
escalates to a human when a fix does not work.

Works fully offline with a deterministic mock LLM provider; OpenAI, Groq and Ollama are
supported through an OpenAI-compatible client.

> Work in progress - full documentation coming with v1.0.0.


## HTTP service and dashboard

```bash
selfheal serve --dev          # http://127.0.0.1:8000  (no API key)
# or: SELFHEAL_API_KEYS=op SELFHEAL_APPROVER_KEYS=apr selfheal serve
```

- `POST /api/runs` – start a packaged scenario (202 async / 200 sync)
- `GET /api/runs/{id}/events?since=N` – poll the event timeline
- `POST /api/runs/{id}/approvals/{incident_id}/approve|reject` – protected actions
- Static dashboard at `/` (topology health, sparklines, timeline, approvals)
- OpenAPI docs at `/docs`

Docker: `docker compose up --build` then `python3 scripts/smoke_test.py`.
Also ships `render.yaml`, `fly.toml`, `railway.json`, and `vercel.json`.
