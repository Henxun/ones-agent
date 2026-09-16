# ONES authentication

The bundled client owns the ONES account-login sequence. Do not reproduce it in prompts, shell snippets, or target-project code.

## Skill-local JSON (copy-and-configure workflow)

When this Skill is copied into a project, edit the `ones.config.json` beside that copy's `SKILL.md`. `scripts/ones_api.py` derives this location from its own `__file__`, so the parent directories may have any names and the command may run from any working directory. No environment variables or `--config` argument are required. A working-directory `ones.config.json` remains a final fallback.

The recommended JSON field names are the lowercase names shown below. For easier migration from environment-variable configuration, the file also accepts flat `ONES_BASE_URL`, `ONES_TEAM_ID`, `ONES_PROJECT_ID`, `ONES_EMAIL`, `ONES_PASSWORD`, `ONES_API_TOKEN`, `ONES_ISSUE_TYPE_ID`, `ONES_DEFECT_ISSUE_TYPE_ID`, and `ONES_REQUIREMENT_ISSUE_TYPE_ID` keys. Do not mix the two forms unless the duplicate values are identical.

```json
{
  "base_url": "https://ones.example.com",
  "team_id": "team-id",
  "project_id": "project-id",
  "issue_types": {
    "defect": "defect-type-id",
    "requirement": "requirement-type-id"
  },
  "auth": {
    "mode": "account",
    "email": "developer@example.com",
    "password": "account-password",
    "password_env": ""
  },
  "timeout_seconds": 30
}
```

For token authentication, replace `auth` with:

```json
{
  "mode": "token",
  "token": "api-token",
  "token_env": ""
}
```

Direct `password` and `token` values provide the requested copy-and-configure behavior, but they are plaintext. `ones.config.json` is excluded by the bundled `.gitignore`; never force-add it or paste it into chat. Keep `ones.config.example.json` as the distributable template.

Keep the password entirely inside its JSON quotes with no accidental leading or trailing spaces. The client rejects edge whitespace before making a login attempt because the ONES password UI does not accept whitespace there. Escape a literal backslash as `\\` according to JSON syntax.

For a safer JSON file, leave the direct secret empty and reference an environment variable:

```json
{
  "mode": "account",
  "email": "developer@example.com",
  "password": "",
  "password_env": "ONES_PASSWORD"
}
```

An alternative file can be selected explicitly. `--config` must precede the subcommand:

```text
uv run <skill-dir>/scripts/ones_api.py --config D:/configs/ones.json auth-check
```

For a stable per-project path without a command argument, set `ONES_CONFIG_FILE` to the absolute configuration path. This variable identifies the file; it does not contain credentials.

## Named profile (system credential store)

Create the default profile interactively:

```text
uv run <skill-dir>/scripts/ones_config.py configure
```

The script prompts for public identifiers and uses a hidden password or API-token prompt. Public values are stored under the user's configuration directory; the credential is stored through the operating system keyring, not in the project or profile JSON.

Verify the saved profile without exposing its credential:

```text
uv run <skill-dir>/scripts/ones_config.py show
uv run <skill-dir>/scripts/ones_api.py auth-check
```

Use named profiles for multiple ONES environments. `--profile` must precede the subcommand:

```text
uv run <skill-dir>/scripts/ones_config.py --profile staging configure --auth account
uv run <skill-dir>/scripts/ones_config.py --profile production configure --auth token
uv run <skill-dir>/scripts/ones_api.py --profile staging projects
```

Import variables already present in the current process into the system-backed profile:

```text
uv run <skill-dir>/scripts/ones_config.py --profile default configure --from-env
```

This reads the variables only inside the configuration process and moves the password or token into the system credential store. It does not create a project `.env` file.

Clear a profile only when explicitly requested:

```text
uv run <skill-dir>/scripts/ones_config.py --profile staging clear --yes
```

## Environment variables (CI or temporary override)

The operator may inject these variables into the agent process:

```text
ONES_BASE_URL
ONES_EMAIL
ONES_PASSWORD
ONES_TEAM_ID
```

Queries filtered by issue type also need `ONES_ISSUE_TYPE_ID`, unless the command supplies `--issue-type`.

As an alternative to account/password login, `ONES_API_TOKEN` may be injected as a bearer token. Environment variables override the Skill-local JSON and selected named profile, which is useful for CI and temporary project/issue-type overrides.

Do not persist `ONES_PASSWORD` or `ONES_API_TOKEN` with `setx`, shell-profile scripts, source-controlled `.env` files, or JSON configuration.

Never ask the user to paste a password into chat, put secrets in a command line, or inspect `.env`. Tell the user which variable names are missing and let them use their secret-management mechanism.

Run a sanitized login probe:

```text
uv run <skill-dir>/scripts/ones_api.py config-check
uv run <skill-dir>/scripts/ones_api.py auth-check
```

`config-check` performs no network request and reports only the selected configuration path, credential source, authentication mode, endpoint origin, and presence of optional query identifiers. `auth-check` then bootstraps the identity session, retrieves the instance's public encryption certificate, performs login, and makes a read-only project query. Neither command returns the email, password, token, certificate, or upstream response body.

## Failure handling

- `configuration`: required public configuration or credentials are missing.
- `authentication`: ONES explicitly rejected authentication or authorization with HTTP 401/403, or the authenticated account has no usable organization.
- `timeout`: ONES did not respond within the configured timeout.
- `not_found`: the requested item cannot be resolved.
- `payload`: ONES returned data that cannot be safely normalized.
- `dependency`: the local script runtime lacks `requests`, `pycryptodome`, or `keyring`.
- `api`: ONES returned another sanitized HTTP/GraphQL failure, or login failed because of connectivity/TLS rather than rejected credentials.

Report the category and remediation. Do not expose upstream response bodies.

For a 401/403, the client may include a strictly validated upstream `errcode` or `code`. Use that code to distinguish CAPTCHA, MFA, lockout, expiry, and credential rejection; do not infer the cause when no code is returned.
