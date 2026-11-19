# How to give cloud sessions secrets with Doppler

Use this guide when a cloud task needs a secret, for example an API key for
its tests, and you keep your secrets in [Doppler](https://www.doppler.com/).
Doppler is one optional provider for the `doppler` secrets mode. Nothing else
in FFS depends on it. If you do not use Doppler, see
[How to give cloud sessions secrets without Doppler](howto-cloud-secrets-other.md).

Cloud delegation is experimental. It is tested with fake provider CLIs. Live
behavior is not yet verified. The Doppler commands below come from the FFS
`env-spec` output and runbook. Anything else about Doppler's own CLI is marked
as unverified at the end of this page.

## How it works

FFS never reads, stores or sends a secret value. The value lives in Doppler.
You give the cloud session one Doppler access token, as the environment
variable `DOPPLER_TOKEN`, in a private environment in the provider's UI. The
session reads secrets through the token. FFS handles names only: the names in
`secrets.exposure`, the `[needs-secret: NAME]` task annotation, and the names
in the `env-check.sh` report.

Every worker session in the environment can read everything that token can
read. So use a dedicated Doppler config for agents, non-production values only,
and a token that is read-only and expires.

## Before you begin

- You finished [How to run your first cloud delegation](howto-cloud-first-run.md)
  up to a green `doctor` with the default `none` secrets mode.
- The Doppler CLI is installed and logged in on your machine, with permission
  to create configs and tokens.
- A private cloud environment exists for each provider you use. Do not share it
  with other repositories.

## Steps

### 1. Create a dedicated Doppler config for agents

Create a branch config of your environment for agents, for example
`<env>_agent`, and put only non-production values in it. `env-spec` uses
this name pattern. Create the config in the Doppler dashboard or with the
Doppler CLI, as the Doppler documentation describes. FFS does not create it.

### 2. Mint a read-only, expiring token

```bash
doppler configs tokens create <name> -p <project> -c <config> --max-age <ttl> --plain
```

`--plain` prints only the token. `<ttl>` is a duration such as `720h`, which is
the value `env-spec` suggests. Copy the output straight into the provider UI in
the next step. Do not write it to a file, a shell history entry or a chat
message. Confirm the token is read-only with `doppler configs tokens create --help`
before you rely on it. Rotate it before it expires.

### 3. Paste the token into the provider environment

In the provider's environment settings, add the variable `DOPPLER_TOKEN`.
Use a private environment.

- Claude: the Claude Code web environment settings. Also allow
  `api.doppler.com` in the network settings. Allow the host that serves the
  Doppler CLI download too. `setup.sh` downloads it from a GitHub release URL
  (`github.com/DopplerHQ/cli/releases`); confirm in your own account which
  hosts the download redirects to.
- Codex: set `DOPPLER_TOKEN` as an environment variable of the Codex
  environment, and allow `api.doppler.com`. `env-spec` says the Codex Secrets
  store is setup-only, so use environment variables for the task phase. Confirm
  this in your Codex settings, because the provider can change it.

### 4. Pin the Doppler CLI in `.ffs/cloud/setup.sh`

`setup.sh` has three placeholders. `env init` does not fill them:

```text
DOPPLER_VERSION="REPLACE_AT_ENV_INIT"
DOPPLER_SHA256_LINUX_AMD64="REPLACE_AT_ENV_INIT"
DOPPLER_SHA256_LINUX_ARM64="REPLACE_AT_ENV_INIT"
```

Replace them with a CLI version and the SHA-256 checksums of that version's
Linux amd64 and arm64 release archives, taken from the Doppler release page
and checked by you. While any value is a placeholder, the CLI is not
installed. The step is recorded as `doppler: unpinned`, counted as failed, and
no setup stamp is written, so every session retries it. The script installs the
CLI to `$HOME/.local/bin` and needs `curl`.

### 5. Edit `.ffs/cloud/config.json`

List every name the token can read. Include `DOPPLER_TOKEN` too: `env-spec`
asks you to list every secret name reachable from the environment, and the token
variable is reachable.

```bash
doppler secrets --only-names -p <project> -c <config>
```

```json
{
  "secrets": {
    "provider": "doppler",
    "wrapper": "doppler run --no-fallback --",
    "install": "",
    "exposure": {
      "claude": ["DOPPLER_TOKEN", "<NAME_1>", "<NAME_2>"],
      "codex": []
    },
    "network_secret_hosts": []
  }
}
```

Keep the other keys of the file as they are. Names should be `UPPER_SNAKE`,
starting with a letter, 64 characters or fewer. The result manifest validator
rejects other forms in the `env_report` names (`MANIFEST_ENV_NAME`). The
`[needs-secret: NAME]` annotation is checked more loosely (letters, digits and
underscores, not starting with a digit, up to 128 characters), and entries in
`secrets.exposure` are only checked to be strings, so a lowercase name is
accepted there but would fail when the worker reports it.

The wrapper must be set. The template default is empty, and with an empty
wrapper the worker is told that secrets are plain environment variables and no
wrapper is needed. With the wrapper set, the worker packet tells it to run every
test and runtime command as `doppler run --no-fallback -- <command>`. A wrapper
is one printable line of at most 200 characters. Check the flags with
`doppler run --help` for your CLI version.

`network_secret_hosts` is in the template for documentation. No code reads it.

### 6. Annotate tasks that use a secret

```text
/swarm --cloud "Run the payments client tests [owns: src/payments/**] [est: 30m] [verify: npm test -- payments] [needs-secret: PAYMENTS_API_KEY]"
```

A task that names a secret is routed only to a provider whose
`secrets.exposure` lists that name. Otherwise it gets the reason
`SECRET_NOT_EXPOSED` and runs locally. `doctor` and `plan --with-doctor` fail with
`SECRETS_DECLARED` when a `needs-secret` name is in no `exposure` list.

### 7. Commit, check and grant

Commit and push `.ffs/cloud/*`, then run:

```bash
bash scripts/gsd/cloud-dispatch.sh doctor
```

With any secret name exposed, `doctor` fails without an active GitHub ruleset on
`ffs/cloud/**`. See
[Security model and stated limits](cloud-delegation.md#security-model-and-stated-limits).

Every exposed name needs a `secret-use:<NAME>` grant before a task is submitted
to that provider, including tasks that use no secret, and each `needs-secret`
name needs one too. An interactive `/swarm` run lists every exposed name in
its single question and records the grants if you say yes. Autonomous and
headless runs never grant for themselves.

## What `env-check.sh` reports

The worker runs `bash .ffs/cloud/env-check.sh <provider>` first and copies the
output unchanged into its result manifest. It prints one JSON object with names
only, never values:

```json
{"tools":{"git":"<version>","doppler":"<version>"},"present":["DOPPLER_TOKEN"],"missing":["<NAME_1>"]}
```

`present` and `missing` split the names in `secrets.exposure.<provider>`.
Entries that start with `host:` are skipped. The script tests its own process
environment. Secrets that only `doppler run` injects are not in that
environment, so they show as `missing` unless the script itself runs under the
wrapper. `DOPPLER_TOKEN` shows as `present` when the provider environment sets
it. `bash .ffs/cloud/env-check.sh <provider> --status` prints the setup status
instead, including each setup step (for example `"doppler":"ok"` or
`"doppler":"unpinned"`) and `"secret_isolation":"env-i-only"`.

## Verification

- `doctor` is green, with `SECRETS_DECLARED` and `RULESET` both `OK`.
- `plan --tasks tasks.json --policy auto --run <run>` shows no missing grants.
- The task manifest's `env_report` lists `DOPPLER_TOKEN` as present. Names the
  token can read are `present` only if the script ran under the wrapper.
- `status --run <run>` shows the task running or integrated, not
  `needs-operator`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `doppler: unpinned` in the setup status | A `REPLACE_AT_ENV_INIT` value is still in `setup.sh`. Fill all three (step 4), commit and push. |
| `doppler: failed` | The download or checksum failed, `curl` is missing, or the architecture is not amd64 or arm64. Check the network settings and the checksums. |
| `SECRETS_DECLARED` FAIL | A `needs-secret` name is in no `exposure` list. Add it. |
| `SECRET_NOT_EXPOSED` in the plan | No usable provider exposes all the task's names. The task stays local. |
| `CLOUD-REFUSED: EXPOSURE_NOT_GRANTED` | A `secret-use:<NAME>` grant is missing. Grant it, or reduce `exposure`. |
| Task blocked `exposure-not-granted`, `ruleset-unverified` or `scan-refused` | Grant it, fix the ruleset or fix the finding, then `resolve --action retest`. |
| `RULESET` FAIL | Add a ruleset on `ffs/cloud/**` that blocks updates and deletions for non-admins. |
| Names show as `missing` | The token cannot read them, or `env-check.sh` ran outside the wrapper. See above. |
| The worker cannot reach Doppler | Allow `api.doppler.com` in the provider's network settings. |
| Secrets stop working after weeks | The token expired. Mint a new one and update the provider environment. |

## If a secret leaks

A leak means a cloud result contained a secret-shaped value, or you suspect one
reached a branch or a session transcript. FFS rejects such a result with the
blocker `secret-leak`, and `status` prints this runbook. The scanner limits
damage after a leak. It does not prevent one. Treat any hit as a leak first.

1. Revoke the agent token now, then rotate the leaked secret itself in Doppler:

   ```bash
   doppler configs tokens revoke <name> -p <project> -c <config>
   ```

2. Delete the remote Git branch that carries the value:

   ```bash
   git push <origin> --delete <branch>
   ```

3. Check the provider session transcript. Delete or unshare the session.
4. Mint a new token (step 2) and update `DOPPLER_TOKEN` in the provider
   environment.
5. If the Doppler branch config held real values, delete or rotate its contents
   too, as the Doppler documentation describes.

FFS did not integrate the leaked result. After you fix the cause, `abandon
--retry` starts a new generation of the task. See
[Runbook: secret leak](cloud-delegation.md#runbook-secret-leak).

## Residual risks

- On Linux, a dependency installed in the cloud session, for example an npm
  or pip install script, can read the environment of its ancestor processes.
  `setup.sh` re-runs under `env -i`, which gives installers a clean environment
  but does not hide the session's tokens. Treat every dependency as able to
  read `DOPPLER_TOKEN`. The real boundary is the provider environment and the
  token's scope.
- A worker that holds a secret can print or push it before FFS scans the
  result.

See [Token hygiene](cloud-delegation.md#token-hygiene) for the short list.

## Not verified

- The exact Doppler CLI flags other than the ones in FFS's `env-spec` output
  and runbook: that `configs tokens create` makes a read-only token by default,
  the arguments of `configs tokens revoke`, and how to create a branch config.
  Check each with `--help` or the Doppler documentation.
- The hosts the Doppler CLI download redirects to, and the network levels the
  provider UIs offer. These belong to the providers and are not read by FFS.
- Live behavior of the `doppler` mode in a real provider session.
