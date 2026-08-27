# GitHub Actions scenario coverage

| ID | Fixture | Detection boundary |
|---|---|---|
| GHA-01 | `gha-01-shell-injection/` | Known PR-controlled context is interpolated directly into `run` |
| GHA-02 | `pr-target-controlled/`, `pr-target-unknown/` | Existing MVP rule for PR-controlled repository code in a privileged job |
| GHA-03 | `gha-03-artifact-trust/` | PR upload and privileged `workflow_run` download/execute chain are all present |
| GHA-04 | `gha-04-mutable-action/` | Privileged job uses an external action ref that is not a full 40-character commit SHA |
| GHA-05 | `gha-05-token-permissions/` | `write-all` is a candidate unless attacker-controlled execution is also proven |
| GHA-06 | `oidc-risk/` | Existing MVP creates an AWS identity candidate; IAM trust remains `UNKNOWN` |
| Inference-01 | `inference-flow/` | Generic input, env, output, execution and credential facts; no scenario-specific Finding rule |
| Inference-Blind | `inference-blind/` | Generic repository-file execution facts connect a PR-controlled local action to a token sink |
| GHA-01..10 matrix | `reasoning-matrix/` | Evaluation-only labels with three variants per major scenario group; no product dispatch |
| Blind composite | `blind-composite/` | Fork PR -> local action -> artifact/cache -> privileged workflow -> Token/OIDC |

Each newly added scenario has `vulnerable/` and `secure/` repositories plus an
`expected.json` oracle. `CANDIDATE` means the local configuration is risky but an
external compromise or runtime condition was not proven. `SUPPORTED` means the
source-to-sink path is supported by local repository evidence; it still does not
mean exploitation was executed or confirmed.

Mutable external refs in an unprivileged job remain a hardening fact and do not create a Finding.

Inference fixtures use GHA-style labels only as evaluation metadata. Product code extracts reusable
facts and the Agent composes those facts into paths; it does not dispatch on fixture IDs.
