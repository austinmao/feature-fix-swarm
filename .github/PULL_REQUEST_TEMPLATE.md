# Pull request

## Problem and outcome

<!-- Who has the problem, what fails today, and what becomes possible? -->

## Changes

<!-- Keep this focused. Explain ownership boundaries when GSD or another integration is involved. -->

## Verification

<!-- List exact commands and results. Include a failing-before/passing-after test for behavior changes. -->

- [ ] Python tests
- [ ] Bats tests
- [ ] ShellCheck and parser checks
- [ ] Skill/model lint, if applicable
- [ ] `npm audit`

## Compatibility and security

<!-- Host, installer, model, sandbox, credential, network, migration, or rollback effects. Write “None” only after checking. -->

## Checklist

- [ ] I updated user-facing docs and examples affected by this change.
- [ ] I did not commit credentials, run state, caches, or local worktree artifacts.
- [ ] I preserved edited/unmanaged install collisions and fail-closed gates.
- [ ] I used exact dependency/action pins where applicable.
