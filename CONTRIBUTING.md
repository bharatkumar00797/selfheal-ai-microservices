# Contributing to SelfHeal

Thanks for your interest in improving SelfHeal. Bug reports, new scenarios, detector or policy
improvements, documentation fixes and pull requests are all welcome.

## Development setup

Requirements: Python 3.11 to 3.13, `git`. Docker is optional (only for the container checks).

```bash
git clone https://github.com/bharatkumar00797/selfheal-ai-microservices.git
cd selfheal-ai-microservices
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Everything runs offline with the default `mock` provider, so no API key is needed for
development or tests. To try a real model, copy `.env.example` to `.env` and set
`SELFHEAL_PROVIDER` / `SELFHEAL_BASE_URL` / `SELFHEAL_API_KEY` / `SELFHEAL_MODEL`.
Never commit `.env` or any key.

## Checks

Run these before opening a pull request; CI runs the same commands on Python 3.11, 3.12 and 3.13.

```bash
ruff check .            # lint
ruff format .           # format (CI uses: ruff format --check .)
mypy                    # strict type checking of src/selfheal
pytest                  # full test suite, offline
```

Optional end-to-end checks:

```bash
selfheal run bad-deploy
selfheal run stuck-database --approver "$USER"
selfheal serve --dev                               # dashboard on http://127.0.0.1:8000
python scripts/smoke_test.py http://127.0.0.1:8000 # all 6 scenarios + approval flow
docker compose up --build --wait                   # hardened container
```

## Guidelines

- Keep the mock provider the default: every feature must work without network access or keys.
- New code is fully typed (`mypy --strict` must pass) and comes with tests.
- Security decisions stay deterministic: the model only picks a hypothesis index; the planner
  and safety policy decide which actions can run. Never let the model invent an action kind or
  free-form parameters.
- Telemetry and deploy notes fed to the model must stay wrapped in `<untrusted-data>` with
  delimiter look-alikes defanged.
- `Cluster.apply()` remains the only mutation path; extend validation there when adding actions.
- Protected services and external services keep their current semantics (approval / immutable).
- Anything touching auth, access modes, rate limiting or the body cap needs a test proving the
  control holds (see `tests/test_api.py`).
- Adding a scenario: extend `scenarios.py` with an `expected_*` block and cover it in
  `scripts/smoke_test.py` plus a healer test.

## Commit style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(policy): clamp scale_out by remaining budget
fix(detector): ignore baseline updates during open incidents
docs: document approval gate for protected services
test: cover red-herring revert on traffic-spike
ci: smoke-test the Docker image
```

Keep commits small and focused, write the subject in the imperative mood, and update
`CHANGELOG.md` under **Unreleased** for user-visible changes.

## Reporting security issues

Please do not open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).
