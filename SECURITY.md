# Security Policy

SelfHeal is an AI-assisted remediation control loop, so bypasses of its safety controls are
treated as vulnerabilities. The design is described in the
[security model](README.md#security-model) section of the README.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | yes |
| < 1.0 | no |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Use GitHub's private vulnerability reporting:
  **Security → Report a vulnerability** on
  <https://github.com/bharatkumar00797/selfheal-ai-microservices/security>.
- Include the affected version or commit, a description of the impact, and steps or a proof of
  concept to reproduce it.

You can expect an acknowledgement within 3 working days and a status update within 10 working
days. Once a fix is released we are happy to credit you in the changelog unless you prefer to
stay anonymous.

## Scope

In scope:

- An action that the safety policy should deny or hold for approval but is allowed (allowlist,
  external-service immutability, cooldown, budget, scale clamp, protected-service gate,
  attempt-cap bypass).
- The model inventing an action kind or free-form parameters that reach `Cluster.apply()`
  without going through the planner and policy.
- Prompt / deploy-note injection that causes an unintended remediation (beyond picking a wrong
  hypothesis index that verification then reverts).
- Resolving an approval twice, as another caller, or without an audit event.
- Authentication or authorization bypass in the REST API, a real provider in public-demo mode,
  or operators approving without an approver key.
- Bypassing rate limits or the request body cap; XSS or CSP bypass in the dashboard.
- Secrets exposed in API responses, logs or the published Docker image.

Out of scope:

- Choosing the wrong hypothesis index when verification + revert still recover (model ranking
  is advisory; the policy and verify loop are the safety boundary).
- Shared public-demo identity when `SELFHEAL_TRUST_PROXY` is unset behind a reverse proxy.
- In-memory state loss across restarts or serverless instances (documented caveat).
- The fact that remediations run against a simulated cluster, not a real control plane
  (documented; real executors are intentionally out of scope for 1.0).
- Vulnerabilities in third-party platforms or dependencies without a demonstrated impact on
  SelfHeal.
