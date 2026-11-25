# Repository rulesets

These files are the branch and tag rules for this repository, kept in git so they are reviewed like code. Import them under **Settings › Rules › Rulesets › New ruleset › Import a ruleset**.

| File | Applies to | Enforces |
| --- | --- | --- |
| `main.json` | the default branch (`main`) | changes arrive by pull request (squash or rebase, conversations resolved); every CI job must pass on an up-to-date branch; linear history; no force pushes; no deletion. Admins may bypass only through a pull request. |
| `release-tags.json` | tags `v*` | release tags cannot be moved, updated or deleted (admins excepted, for emergencies). |

Required approvals are 0 because the project has one maintainer; raise it when there are more. `scripts/tests/test_scripts.py` fails if the required checks drift from the job names in `.github/workflows/ci.yml`.
