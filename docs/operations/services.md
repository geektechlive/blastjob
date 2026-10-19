# Running the services

**What this covers:** Keeping the cockpit always on and running the daily job on a schedule, on macOS with launchd or on Linux with systemd or cron. For whoever runs blastjob on a machine.

You do not need either service to use blastjob. Without them you start the cockpit by hand and run the daily job when you want it. The services matter if you want the digest to arrive every morning and its cockpit links to open without you starting anything.

## Running the cockpit by hand

```bash
uv run blastjob-web
```

This binds the address and port from your settings (`web.host`, default `127.0.0.1`, and `web.port`, default 8765), opens a browser tab and prints the address. Three flags override the settings for one launch:

- `--no-browser` does not open a tab. Use it for services.
- `--port 8765` sets the port.
- `--host 127.0.0.1` sets the bind address. Anything other than the local machine exposes the cockpit to your network; read [using the cockpit from another device](lan-access.md) first.

```bash
uv run blastjob-web --no-browser --port 8765
```

Settings are read once when the cockpit starts, so a change to `web.host` or `web.port` takes effect after a restart. Other settings are picked up without one.

When the cockpit starts it also marks any build that a crash left half done as failed, so the job can be tailored again.

## Installing the services (macOS)

From the checkout:

```bash
sh scripts/install-launchd.sh          # cockpit always on, plus the 07:30 daily run
sh scripts/install-launchd.sh web      # only the cockpit
sh scripts/install-launchd.sh daily    # only the daily run
```

The script fills in the templates in `launchd/` for this checkout and your user, writes them to `~/Library/LaunchAgents/`, and loads them. Running it again replaces and reloads the same services, so it is safe to repeat. The services are labelled `local.<your user name>.blastjob.web` and `local.<your user name>.blastjob.daily`. To use a different prefix, set `LABEL_PREFIX` when you run it.

If another blastjob service with a different label is already installed or loaded (an older install, say), the script stops and tells you. Two cockpits or two daily runs would work on the same data. Unload the old one as the message shows, or run the script with `--replace` to have it unload and remove the others:

```bash
sh scripts/install-launchd.sh --replace
```

If `BLASTJOB_DATA_DIR` is set when you run the script, it is passed on to the services and their logs go in that folder. Set `RENDER_ONLY=1` to print the finished files without installing anything.

The services start the programs in the checkout's `.venv` folder, which `uv sync --extra dev` creates. Install and run `uv sync` first.

## The cockpit service

The cockpit service starts at login, starts `scripts/run-web.sh`, and is restarted automatically if it stops (at least 10 seconds apart). `scripts/run-web.sh` runs `blastjob-web --no-browser`. It reads `web.host` and `web.port` from your settings when it starts. After changing either, restart the service:

```bash
launchctl kickstart -k gui/$(id -u)/local.$USER.blastjob.web
```

(`local.$USER` is the default label prefix. Use your own if you changed it.)

## The daily run service

The daily service runs `scripts/run-daily.sh` at 07:30 each day. That runs `blastjob-daily`: it finds new jobs, scores them, checks postings, and sends one digest. [The daily run](../how-it-works/daily.md) describes each step and [the digest](../using/digest.md) shows what you receive.

It is a launch agent, so it runs as you and only while you are logged in. It must run on the machine that holds your data, and with no API key it needs the same `claude` login you use for tailoring.

These commands are useful by hand:

```bash
uv run blastjob-daily --dry-run     # print the digest; send and stamp nothing
uv run blastjob-daily --status      # the last recorded run
touch ~/.blastjob/PAUSE             # kill switch: the daily run exits at once
```

To resume, delete the `PAUSE` file. It lives in your data folder (`BLASTJOB_DATA_DIR`, default `~/.blastjob`). What a pause skips is in [the kill switch](../how-it-works/daily.md#kill-switch).

## Logs

All logs are in your data folder:

- `daily.log` holds a start line and a finish line (with the exit code) for every daily run, and the run's own output between them. Start here when a digest did not arrive.
- `daily.launchd.log` and `web.launchd.log` hold anything the two services print outside that. The cockpit's output goes to `web.launchd.log`.

If the digest mode is `stdout` (the default), the digest itself is printed into `daily.log`.

## Linux

The launchd files do nothing on Linux. Run the same two programs from systemd or cron, from the checkout:

```bash
uv run blastjob-web --no-browser
uv run blastjob-daily
```

For example, a cron line for the daily run at 07:30:

```
30 7 * * * cd /path/to/blastjob && uv run blastjob-daily >> ~/.blastjob/daily.log 2>&1
```

For the cockpit, write a systemd service whose command is `uv run blastjob-web --no-browser`, with the checkout as its working directory and `Restart=on-failure`. blastjob does not ship one. Make sure the service's user can run `uv` and, if you use the `claude` tool, has logged in.

## After an upgrade

If an upgrade changes the launchd templates, run `sh scripts/install-launchd.sh` again and restart the cockpit service. Then run `uv run blastjob setup doctor`. The full upgrade steps are in [your data and backups](data-and-backups.md#upgrading).
