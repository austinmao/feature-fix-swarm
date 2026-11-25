# How to give cloud sessions secrets without Doppler

Doppler is optional. `secrets.provider` in `.ffs/cloud/config.json` has three
values that the code names: `none` (the default), `doppler` and `command`.
`provider-env` is the name these docs use for the plain case where you put the
values in the provider environment yourself. The code does not name it, and it
is not validated. Any `secrets.provider` value other than `none`, `doppler` or
`command` is accepted and behaves the same way, which is also how `provider-env`
behaves:

- The worker packet is built as for any non-`none` value: it carries your
  `secrets.wrapper` if you set one (otherwise it says secrets are plain
  environment variables and no wrapper is needed), and the secret names the
  task may use.
- `setup.sh` runs no install step. Only `doppler` (installs the Doppler CLI)
  and `command` (runs `secrets.install`) trigger one.

Only `none` turns the secrets text in the worker packet off. Open-source and
personal projects usually need only `none` or the `provider-env` setup. This
page covers every mode except `doppler`, which has its own guide:
[How to give cloud sessions secrets with Doppler](howto-cloud-secrets-doppler.md).

Cloud delegation is experimental. The Claude backend ran live once, on one
trivial task. The secrets modes have not run live. Statements about what a provider's UI offers
come from FFS's `env-spec` output and are marked where you must confirm them.

## Rules that apply in every mode

- FFS never reads, stores or sends a secret value. It handles names only.
- Exposure is the whole environment. Every worker session sees everything the
  provider environment exposes. So `secrets.exposure.<provider>` must list every
  secret name reachable there, not only the ones a task uses. Names should be
  `UPPER_SNAKE`, starting with a letter, 64 characters or fewer. The result
  manifest validator rejects other forms in the `env_report` names; the
  `[needs-secret:]` annotation and `secrets.exposure` are checked more loosely.
- Each listed name needs a `secret-use:<NAME>` grant before any task is
  submitted to that provider, including tasks that use no secret. A missing grant
  stops the run at `start` (autonomous) or needs your yes (interactive, which
  lists the names).
- A task names the secrets it uses with `[needs-secret: NAME]`. It is routed
  only to a provider whose `exposure` covers every name. Otherwise it gets the
  reason `SECRET_NOT_EXPOSED` and runs locally.
- Use dedicated, non-production values and a private provider environment.
- With any name exposed, `doctor` fails without an active GitHub ruleset on
  `ffs/cloud/**` that blocks updates and deletions for non-admins.

The first-run steps are in [How to run your first cloud delegation](howto-cloud-first-run.md).
For Claude they include accepting the folder trust dialog once
(`FFS_CLOUD_CLONE_PARENT`). The full reference is in [Secrets](cloud-delegation.md#secrets).

## Mode `none`

This is the default. Leave `exposure` empty:

```json
{ "secrets": { "provider": "none", "exposure": { "claude": [], "codex": [] } } }
```

Workers get no secrets, and the worker packet has no secrets wrapper line. A task
that needs a secret stays local. Mark it `[local]`, or switch to another mode.
Sandboxed local tests have no network.

## Mode `provider-env`

Workers read the values as environment variables of the cloud session. There is
no wrapper. `provider-env` is a label, not a value the code recognises: any
unrecognised `provider` value behaves identically (see the top of this page).
You can leave `provider` as `none` and only list names in `exposure`, but then
the worker packet carries no secrets text, so a non-`none` value is recommended
when you expose names.

1. Set `"provider": "provider-env"` (any value other than `none`, `doppler` or
   `command` has the same effect).
2. Put each value in the provider environment:
   - Claude: environment variables of the Claude Code web environment. For HTTP
     API keys you can use the provider's network secrets instead.
   - Codex: environment variables of the Codex cloud environment. `env-spec` says
     the Codex Secrets store is available only during environment setup, not
     during the task phase. That is per the provider's documentation at the time
     of writing. Confirm it in your own Codex environment settings before you
     rely on it.
3. List every name in `secrets.exposure.claude` and/or `secrets.exposure.codex`.
   For a Claude network secret, list it as `host:<name>`. It is granted as
   `secret-use:host:<name>` and skipped by `env-check.sh`.
4. Annotate tasks with `[needs-secret: NAME]`.
5. Commit and push `.ffs/cloud/*`, then run `doctor`.

```json
{
  "secrets": {
    "provider": "provider-env",
    "exposure": { "claude": ["<NAME_1>"], "codex": ["<NAME_2>"] }
  }
}
```

The worker packet then says that secrets are environment variables of the
session and that no wrapper is needed, and it lists the names the task may use.

## Mode `command`

Use this for any other secrets tool, such as 1Password or Infisical. You supply
two commands.

1. Set `"provider": "command"`.
2. Set `secrets.wrapper` to the prefix that injects secrets and then runs the
   command that follows it. One printable line, 200 characters at most.
   Examples of the shape, with flags you must check against your tool's own
   documentation:

   ```text
   op run --
   infisical run --
   ```

   The worker packet tells the worker to run every test and runtime command as
   `<wrapper> <command>`.
3. Set `secrets.install` to the command that installs your secrets CLI. Use the
   install command from your tool's documentation. `setup.sh` runs it from the
   repository root through `bash -c`, as part of session setup, with a scrubbed
   environment (`env -i`):

   ```json
   {
     "secrets": {
       "provider": "command",
       "wrapper": "<your-cli> run --",
       "install": "<command that installs your-cli>",
       "exposure": { "claude": ["<TOKEN_NAME>", "<NAME_1>"], "codex": [] }
     }
   }
   ```

4. Put the tool's access token in the provider environment as an environment
   variable. List that variable's name, and every name the tool can read, in
   `exposure`.
5. Use a read-only, expiring token, scoped to the smallest set of secrets the
   tasks need. Rotate it.
6. Commit and push `.ffs/cloud/*`, then run `doctor`.

`env-check.sh` tests its own process environment. Values that only the wrapper
injects show as `missing` unless the script itself runs under the wrapper.
Names only are ever printed.

On Linux, `env -i` is not a secret boundary. An installer that runs in the
session can read the environment of its ancestor processes. Treat every
dependency installed in the session as able to read the secrets it holds.

## Verification

- `doctor` is green. `SECRETS_DECLARED` is `OK` and `RULESET` is `OK`.
- `bash scripts/gsd/cloud-dispatch.sh plan --tasks tasks.json --policy auto --run <run>`
  shows `cloud` routing for the task and no missing `secret-use:` grants.
- With `provider-env` (or any unrecognised value), the manifest `env_report` lists your names under
  `present`. In `command` mode, expect them under `present` only if
  `env-check.sh` ran under the wrapper.
- `bash .ffs/cloud/env-check.sh <provider> --status`, run in a session, prints
  the setup status. For `command` mode, the step `secrets-install` is `ok`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Task stays local with `SECRET_NOT_EXPOSED` | The provider's `exposure` does not list every `needs-secret` name. Add it, grant it, commit and push. |
| `SECRETS_DECLARED` FAIL | A `needs-secret` name is in no `exposure` list. |
| `CLOUD-REFUSED: EXPOSURE_NOT_GRANTED` | A `secret-use:<NAME>` grant is missing. Grant it, or reduce `exposure`. |
| `CLOUD-REFUSED: PACKET_INVALID` | `secrets.wrapper` is longer than 200 characters or not one printable line. |
| `RULESET` FAIL | Add a ruleset on `ffs/cloud/**` that blocks updates and deletions for non-admins. |
| Names show as `missing` | The value is not set in the provider environment, or it is injected only by the wrapper. |
| `secrets-install` is `failed` | The install command failed. Check it runs from a clean environment (`env -i`) on Linux. |
| Codex tasks cannot see a secret | The value was stored in the Codex Secrets store, which is setup-only per `env-spec`. Use an environment variable. |

## If a secret leaks

FFS rejects a result that contains a secret-shaped value (blocker
`secret-leak`). Treat any hit as a leak first.

1. Revoke or rotate the credential now, in your secrets tool.
2. Delete the remote branch that carries it: `git push <origin> --delete <branch>`.
3. Check the provider session transcript and delete or unshare the session.
4. Mint a new token and update the provider environment.

See [Runbook: secret leak](cloud-delegation.md#runbook-secret-leak).

## Not verified

- Which flags `op run` and `infisical run` need, and the install command for
  each. The two wrapper examples are shapes, not tested configurations, and
  they are not taken from the FFS repository. Use your tool's documentation.
- That Codex Secrets are setup-only. `env-spec` states it per the provider's
  documentation. The operator must confirm it.
- The network levels and secret features that the Claude and Codex UIs offer.
- Live behavior of any secrets mode in a real provider session. None has run live.
