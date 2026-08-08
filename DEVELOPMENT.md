# HA Compatibility Manual Merge Checklist

Use this checklist to decide whether a PR risks breaking compatibility with supported Home Assistant versions, without relying on automated checks.

## PR Label Meanings (Dependabot)

Use labels as a quick first-pass signal for review scope and compatibility risk:

- `dependencies`: General dependency update PR (always dependency-related).
- `devcontainer`: Dev container/tooling dependency updates. Usually lower HA runtime risk, but still review for local dev/test environment impact.
- `github-actions`: CI workflow dependency updates (GitHub Actions ecosystem). Usually low runtime risk, but can affect validation/test reliability and release safety.
- `python`: Python package dependency updates. Highest chance of affecting HA compatibility, especially when auth/session/networking, parsing, recorder/statistics, or Home Assistant-adjacent libraries are involved.

Label combinations:

- `dependencies` + `python`: treat as runtime compatibility-sensitive by default.
- `dependencies` + `github-actions`: treat as CI/process-sensitive; verify workflow behavior does not hide compatibility regressions.
- `dependencies` + `devcontainer`: treat as developer-environment-sensitive; usually mergeable with lighter runtime concern unless shared tooling behavior changes.

## PR Title Auto-Labeler Text Matches

Auto-labeling is based on PR title text patterns configured in `.github/release-drafter.yml`.

| Label     | PR title text the autolabeler looks for                                                                                  | Example title                                   |
| --------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| `major`   | Conventional commit type with breaking marker: `type(scope)!:`                                                           | `feat(api)!: change login payload format`       |
| `minor`   | Starts with `feat:` or `feat(scope):`                                                                                    | `feat(coordinator): add combined import stream` |
| `patch`   | Starts with one of: `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `ci`, `build`, `style` (with optional `(scope)`) | `fix(api): handle 503 login response`           |
| `feature` | Starts with `feat:` or `feat(scope):`                                                                                    | `feat(button): add manual refresh control`      |
| `bug`     | Starts with `fix:` or `fix(scope):`                                                                                      | `fix(config_flow): map portal error correctly`  |
| `chore`   | Starts with one of: `chore`, `docs`, `refactor`, `perf`, `test`, `ci`, `build`, `style` (with optional `(scope)`)        | `docs: update HA compatibility checklist`       |

Notes:

- Multiple labels can be added from one title (for example, `feat:` adds both `minor` and `feature`).
- `major` detection uses the `!` breaking-change marker in the type prefix.
- If a title does not match these patterns, auto-labeling will not add these semantic labels.

## 1. Change Type Risk Classification

Classify the PR before deciding merge risk:

- Low risk: docs-only, comments, non-runtime dev tooling.
- Medium risk: test framework updates, CI-only workflow updates, formatting/lint tool updates.
- High risk: Home Assistant version-related changes, auth/session/network dependency updates, recorder/statistics logic, config flow behavior, coordinator behavior, manifest/integration metadata changes.

If classified as high risk, complete Section 2 and Section 3 before merge.

## 2. Manual HA-Focused Validation (For Medium/High Risk)

- [ ] Run local checks:
  - `scripts/lint`
  - `scripts/test`
- [ ] Verify integration setup still works on a clean restart.
- [ ] Verify config flow login succeeds and does not regress error handling.
- [ ] Verify first coordinator refresh succeeds.
- [ ] Verify Recorder statistics are still imported and visible where expected.
- [ ] Verify at least one HA-visible surface after change (entity/service/statistic visibility path).

## 3. Dependency-Specific Compatibility Review

For dependency PRs, check these compatibility questions:

- [ ] Does this update touch Home Assistant core, aiohttp/session/auth, or parser behavior?
- [ ] Could this update change API request/response handling used by the integration?
- [ ] Could this update affect Recorder statistics APIs or long-term statistics behavior?
- [ ] Is this a major version bump that needs release notes/changelog review before merge?

If any answer is yes and evidence is weak, do not merge until validated.

## 4. Stop-Merge Conditions

Do not merge when any of these are true:

- [ ] Login/setup/first refresh behavior is unverified after high-risk changes.
- [ ] Recorder import/statistics visibility is unverified after data-path changes.
- [ ] Reviewer cannot explain why the change is safe for the minimum supported HA version.

## 5. Merge Decision

Merge only when:

- [ ] Risk classification is documented in PR review notes.
- [ ] Required manual validation is complete for the PR risk level.
- [ ] There is clear evidence the PR does not break supported HA compatibility.
