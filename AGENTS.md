# Agent Instructions

This file helps AI coding agents become productive quickly in this repository.

## Scope

- Home Assistant custom integration code lives in [custom_components/sapowernetworks](custom_components/sapowernetworks).
- Runtime Home Assistant config for local development lives in [config](config).
- Tests live in [tests](tests).

## Fast Start Commands

- Install dependencies: run scripts/setup
- Start Home Assistant dev instance: run scripts/develop
- Run lint and auto-fixes: run scripts/lint
- Run tests: run scripts/test

## Workflow Expectations

- Before coding, read [README.md](README.md)
- Prefer minimal edits and keep changes scoped to the user request.
- Validate with scripts/test for behavior changes and scripts/lint for style changes.
- When user-facing names, icons, entity IDs, or labels change, verify the final Home Assistant-visible surface before wrapping up.
- When editing dashboard examples or documentation rendering, sanity-check the rendered YAML/Markdown output, not just the source text.
- Run the narrowest useful test or lint command early, fix the first failing slice, and only widen scope after that slice passes.
- Keep commits on a feature branch rather than `main`.
- Keep changes commit-ready when the user asks for a commit, and prefer conventional commit messages for repo history.
- For terse follow-up prompts, use deterministic handling:
	- `errors`: run the narrowest relevant failing check immediately, report the first failure clearly, then fix and re-run.
	- `continue`: execute the next planned implementation slice without restating the entire plan.
	- `is X there?`: verify directly in HA/logs/recorder state and answer with evidence.
- For auth/login failures, do not classify HTTP 503 as outage/transient by default. First inspect response shape/content, compare against prior successful auth in the same run, and only then decide between portal outage vs integration request-shape/auth-session issue.
- After any user-visible integration change, run a Home Assistant visibility check before wrapping up:
	- verify entity/service creation,
	- verify recorder statistics presence where applicable,
	- provide exact HA navigation path where the user should see the result.
- Treat `hacs.json` as the single source of truth for minimum Home Assistant version compatibility. Do not add unsupported metadata keys to integration manifest files.

## Commit Protocol

- When the user asks for `commit`, first run `git status --short` and confirm there are staged or stageable changes relevant to the request.
- If currently on `main`, create/switch to a feature branch before committing.
- If there is nothing to commit, report cleanly and propose the next concrete action instead of attempting an empty commit.
- Prefer one focused conventional commit message per logical slice.

## Architecture Notes

- Entry setup and platform forwarding: [custom_components/sapowernetworks/__init__.py](custom_components/sapowernetworks/__init__.py)
- API client and typed API errors: [custom_components/sapowernetworks/api.py](custom_components/sapowernetworks/api.py)
- Coordinator polling and error mapping: [custom_components/sapowernetworks/coordinator.py](custom_components/sapowernetworks/coordinator.py)
- Config flow: [custom_components/sapowernetworks/config_flow.py](custom_components/sapowernetworks/config_flow.py)
- Domain constants: [custom_components/sapowernetworks/const.py](custom_components/sapowernetworks/const.py)

## Project Conventions

- Use async-first Home Assistant patterns and typed exceptions in the API layer.
- Keep imports and typing style consistent with existing files.
- Keep tests aligned with pytest-homeassistant-custom-component fixtures in [tests/conftest.py](tests/conftest.py).

## Known Pitfalls

- scripts/develop bootstraps dependencies if Home Assistant modules are missing; prefer running scripts/develop from repo root.
- Root endpoint behavior should be validated against Home Assistant startup logs in [config/home-assistant.log](config/home-assistant.log) when troubleshooting 404 or startup failures.
- pytest async fixture compatibility depends on [pytest.ini](pytest.ini) using asyncio_mode = auto.
- `custom_components/*/manifest.json` schema can reject extra keys depending on current HA validator rules; verify compatibility metadata policy in `hacs.json` and CI checks before adding manifest fields.

## Test Guidance

- Prefer MockConfigEntry based tests for setup, unload, and config flow behavior.
- Patch IntegrationBlueprintApiClient async methods instead of calling external APIs.
- Keep tests deterministic and avoid network calls.

## References

- Home Assistant developer docs: https://developers.home-assistant.io/docs/creating_component_index
- DataUpdateCoordinator pattern: https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
- Test framework docs: https://github.com/MatthewFlamm/pytest-homeassistant-custom-component
