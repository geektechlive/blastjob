# The setup doctor

**What this covers:** What `blastjob setup doctor` checks, how to read its output, and how to fix what it reports. For anyone installing, upgrading or troubleshooting.

## Running the doctor

```bash
uv run blastjob setup doctor
```

Run it after installing, after an upgrade, and whenever something stops working. It spends no model tokens and sends nothing. It does open the database in your data folder, and creates it on a fresh install.

Each line starts with a result and a check name, then details. A failed or warning line is followed by a `fix:` line saying what to do.

```
[ok  ] database writable -- /home/jane/.blastjob/blastjob.db
[FAIL] candidate.target_titles set
       fix: blastjob setup set candidate.target_titles '["Your Title"]'
[warn] secrets file -- /home/jane/.blastjob/secrets.env
       fix: create /home/jane/.blastjob/secrets.env (chmod 600) with KEY=VALUE lines, if you use any keys
```

The command exits with 1 if any required check failed and 0 otherwise. Warnings never change the exit code.

## What it checks

Required checks (a failure is `FAIL`):

- **database writable** and **migrations current**. The database can be written and its schema is up to date. Any blastjob command migrates it, so the fix is to run one.
- **settings valid**. Every saved setting passes validation. A failure names the key; correct it with `blastjob setup set`.
- **work_history.md present**. Your work history exists. If not, start the cockpit and ingest your resumes (see [install](../getting-started/install.md#add-your-work-history)).
- **candidate.target_titles set**. At least one target title is set. Nothing is scored until it is.
- **LLM provider**. A model can be used: the `claude` tool is on your path and runs, or the API key variable for Anthropic or OpenAI is set. This is a local check; it makes no model call.
- One line per secret your settings need, named **`<VARIABLE> set`**. The doctor lists only secrets for features you turned on: the alert mailbox if mail alerts are enabled, the personal mailbox if its variable names are set, Adzuna if aggregators are enabled, and the Discord token and channel if the digest mode is `discord`. Each must be in the environment or in your secrets file.

Warnings (a failure is `warn` and does not fail the run):

- **secrets file**. The file named by `paths.secrets_file` (default `<data folder>/secrets.env`) does not exist. Fine if you use no keys.
- **secrets file mode**. The secrets file can be read or written by other users, or belongs to another user. Fix it with `chmod 600` on the file. Shown only when the file exists.
- **unknown settings**. The database holds settings this version does not know. They are kept and ignored. Remove them only if you know they are obsolete.
- **`JINA_API_KEY set`**. Shown when search firms or Apple discovery are on. Those need the key to search.
- **`<setting> naming`**, for example `mail_user_env naming`. One line per saved mail variable name that breaks the [mail naming rule](settings-and-secrets.md#mail-accounts). To clear the warning, rename the variable in your secrets file and on the **Settings** page. The doctor does not print the name itself.

## Reading the results

Fix `FAIL` lines first, top to bottom, then run the doctor again. A fresh install fails several checks at once; that is normal.

Once your work history and target titles are in, whatever remains are warnings about features you have not turned on, which you can ignore.

If a secret line fails, the variable is not in the environment or in your secrets file. Add `NAME=value` to the file; never put the value in settings ([why](settings-and-secrets.md#secret-names-in-settings)).

## Checking a checkout (contributors)

The doctor checks an installation. To check the code itself before you change or share it, run the local gate:

```bash
sh scripts/verify.sh
```

It runs the linter, the format check, the type checker, the tests and the personal-data check, and stops at the first failure. See [CONTRIBUTING.md](../../CONTRIBUTING.md).
