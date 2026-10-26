# Settings and secrets

**What this covers:** How settings are changed, where secrets live, why settings hold only secret names, the optional keys and what each unlocks, mail accounts, and importing an old config. For anyone configuring an install.

## Changing settings

Settings are stored in your database (`blastjob.db` in the data folder), one value per dotted name such as `discovery.comp_floor_usd`. You can change them in two places.

**The Settings page** in the cockpit (`/settings`) is a form for the common ones.

**The command line** reaches all of them:

```bash
uv run blastjob setup get                          # every setting, as TOML
uv run blastjob setup get discovery.comp_floor_usd # one setting, as JSON
uv run blastjob setup set discovery.home_state OH
uv run blastjob setup export                       # the settings you have saved, as TOML
uv run blastjob setup export --all                 # plus every default
```

How `setup set` reads a value:

- Plain words and numbers are written as they are: `120000`, `OH`.
- Booleans are `true` or `false`.
- Lists and objects are JSON, inside single quotes so the shell leaves the double quotes alone: `'["Data Engineering Manager", "Head of Data"]'`.
- For a setting that is a list of text items, a bare string becomes a one-item list, and the command says so.

`setup set` checks the whole result before saving. An unknown name or an out-of-range value is refused with a message saying why, and nothing is saved. Changes apply to the next page load or daily run without a restart, except `web.host` and `web.port`, which are read when the cockpit starts. One setting, `paths.data_dir`, cannot be set this way; point the `BLASTJOB_DATA_DIR` environment variable at the folder instead.

`web.public_url` sets the address the digest's cockpit links use; it is checked the same way. Saving `web.host` as anything but a local address prints a warning. See [using the cockpit from another device](lan-access.md).

`notify.recruiter_touch_days` (default 42, whole days from 14 to 120) and `notify.recruiter_lookback_days` (default 365, from 90 to 1,095) shape the digest's [recruiters to touch](../how-it-works/daily.md#recruiters-to-touch) block. They are on the settings page under "Recruiter touch-again reminder" and "Recruiter reminder lookback". An out-of-range value is refused by both.

`notify.quiet_after_days` (default 21, whole days from 7 to 90) and `notify.pace_weekday` (`mon` to `sun`, or `off`; default `mon`) shape the digest's [quiet applications](../how-it-works/daily.md#quiet-applications) nudge and [weekly pace line](../how-it-works/daily.md#weekly-pace-line). They are on the settings page under "Quiet application, days" and "Weekly pace line". Both refuse an out-of-range value; if a bad value is already stored, the quiet threshold falls back to 21 and logs a warning.

`limits.daily_deadline_minutes` (default 45, whole minutes from 5 to 240) is how long the daily run may keep starting work; see [the deadline](../how-it-works/daily.md#one-run-at-a-time-and-the-deadline). It is also on the settings page under "Daily run time limit". An out-of-range value is refused by both.

The other limits keep a model from running away ([the call ledger and budget](../how-it-works/llm.md#the-call-ledger-and-budget)). Each is whole numbers, validated on the settings page, `setup set` and a TOML import alike, and a save of one never resets the others:

| Setting | Default | Range | Settings page label |
|---|---|---|---|
| `limits.scheduled_calls_per_day` | 150 | 1 to 5000 | "Scheduled model calls per day" |
| `limits.clicked_calls_per_day` | 300 | 1 to 5000 | "Clicked model calls per day" |
| `limits.cockpit_concurrency` | 3 | 1 to 8 | "Cockpit runs at once" |
| `limits.sdk_timeout_seconds` | 300 | 30 to 900 | "API call timeout" |

A call over a daily cap is refused, not queued. The daily run says it stopped at its limit ([daily](../how-it-works/daily.md#the-call-budget)). Cockpit runs beyond `cockpit_concurrency` wait and show as queued ([web](../how-it-works/web.md#running-now)).

To pause the daily run without changing any setting, create a file named `PAUSE` in the data folder ([services](services.md#the-daily-run-service)), or use "Pause scheduled runs" on the cockpit's Running now page, which makes and deletes the same file.

## The secrets file

API keys and app passwords live in a plain text file, never in the repository and never in your settings. The setting `paths.secrets_file` names it; if empty it is `<data folder>/secrets.env`. Make it readable only by you (`chmod 600`). blastjob still loads a file that other users can read or write, or that another user owns, but logs a warning (never the contents) and `blastjob setup doctor` shows a `secrets file mode` warning.

The file has one `KEY=value` per line. Blank lines and lines starting with `#` are ignored, and quotes around a value are removed:

```
JINA_API_KEY=your-key-here
ADZUNA_APP_ID=your-id
ADZUNA_APP_KEY=your-key
```

blastjob reads the file by itself on start, so you do not need to load it into your shell. If a variable is already set in your environment, the environment wins over the file. If you move the file, the new path must be absolute and outside the blastjob checkout, where it could be committed; `setup set paths.secrets_file` refuses anything else. Do not paste secret values into chat, issues or settings.

## Secret names in settings

A setting that needs a secret holds only the name of an environment variable. For example `discovery.mail_password_env` holds `ALERT_IMAP_APP_PASSWORD`, and the password is on a line with that name in the secrets file. This way, exporting or sharing your settings never leaks a secret, and a password pasted into the wrong box cannot be saved.

Settings whose names end in `_env` accept only something shaped like a variable name: capital letters, digits and underscores, starting with a letter. `setup set` and the Settings page both refuse anything else, such as a real password, and say so. Leaving the secret out is fine; the [doctor](doctor.md) reports any name your settings use that the environment and the file are missing.

## Optional keys

Each of these unlocks one extra and none is required. Put them in the secrets file.

| Key | What it unlocks |
|---|---|
| `JINA_API_KEY` | Finding people at a company, company research, search-firm and Apple discovery, and checks of employer careers pages when deciding if a posting closed |
| `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` | The Adzuna job aggregator (free keys; turn it on with `discovery.enable_aggregators`) |
| An IMAP app password (names are yours) | Job-alert email from LinkedIn and Indeed, and recruiter-reply scanning; see below |
| `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` | Sending the daily digest to Discord (`notify.mode` set to `discord`) |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | Using that model instead of the `claude` tool. The Anthropic key also adds prompt caching and live web search |

The variable names in the second, fourth and fifth rows are the defaults. Each can be changed with its `_env` setting.

## Mail accounts

blastjob reads up to two mailboxes, and they are different jobs:

- The **alert mailbox** receives LinkedIn and Indeed job-alert emails. It is read by the daily run to find new postings.
- The **personal mailbox** is where recruiters write to you. It is scanned to surface replies from companies you applied to. If you leave a field blank, it uses the alert mailbox's value.

For each, use an app password rather than your account password. In settings write only the variable names, then add the values to the secrets file:

```bash
uv run blastjob setup set discovery.mail_alerts_enabled true
uv run blastjob setup set discovery.mail_imap_host imap.example.com
uv run blastjob setup set discovery.mail_user_env ALERT_IMAP_USER
uv run blastjob setup set discovery.mail_password_env ALERT_IMAP_APP_PASSWORD
```

Then add `ALERT_IMAP_USER=...` and `ALERT_IMAP_APP_PASSWORD=...` to the secrets file. The personal mailbox uses `discovery.personal_mail_imap_host`, `discovery.personal_mail_user_env` and `discovery.personal_mail_password_env`.

The **Mail accounts** section of the Settings page edits the same fields and shows, for each mailbox, whether each variable is **set** or **missing**. It never shows a value. Its **Test connection** button logs in to the saved account to check the credentials and changes nothing.

Naming rule: a mail user variable must end in `_IMAP_USER`, a password variable in `_IMAP_PASSWORD` or `_IMAP_APP_PASSWORD`, and a mail variable may not share a name with any other `_env` setting. This stops anyone who can reach the cockpit from pointing a mail login at the host of their choice with, say, your `ANTHROPIC_API_KEY` as the "password". The Settings page enforces the rule when you save. `setup set` checks only that the name is shaped like a variable, so use matching names there too. An older saved name that breaks the rule keeps working, and the [doctor](doctor.md) warns about it.

## Importing an old config

Settings used to live in a `config.toml` file. On first start with an empty settings table, blastjob imports an old `config.toml` once, if it finds one, and leaves the file in place as a backup. You can also import a file yourself, as many times as you like:

```bash
uv run blastjob setup import path/to/config.toml
uv run blastjob setup doctor
```

Every recognized key is saved after checking, in one step. Unknown keys are kept and reported, not dropped. If the old config set a custom `paths.data_dir`, point `BLASTJOB_DATA_DIR` at that folder instead. Anything the old setup kept in code rather than in the config, such as locations, title filters or your profile, has to be entered again; see [install](../getting-started/install.md#tell-it-what-you-want).
