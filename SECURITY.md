# Security

## Reporting a problem

Please report security problems privately, not in a public issue. Use the
**Report a vulnerability** button on this repository's **Security** tab
(GitHub private vulnerability reporting). Include what you found, how to
reproduce it, and what an attacker would gain.

## What blastjob is built to resist

blastjob is a personal tool that runs on your own computer. It is not meant to be
exposed to the internet.

- The web cockpit listens on `127.0.0.1` by default, so only your own computer can
  reach it.
- A web page you visit must not be able to read from or change the cockpit. It
  checks the Host and Origin headers and requires a per-launch CSRF token on every
  change. A way around any of these is a bug worth reporting.
- Job postings, alert emails and model output are untrusted. A posting that gets
  the app to run script, fetch a private address, reach a tool, or leak a secret
  is a bug worth reporting.
- The cockpit has **no login** (see [LAN access](docs/operations/lan-access.md)). If you set `web.host` to `0.0.0.0` so other
  devices on your network can reach it, anyone on that network can use it. That
  is a documented choice, not a vulnerability on its own. Something that lets a
  network user go further, such as pulling secret values out of the app, is still
  worth reporting.

## Agent instruction files

`AGENTS.md` and `CLAUDE.md` are loaded by AI coding agents as instructions, so a
change to either one in a pull request can steer any agent that runs on that
branch. Review those changes before running an agent on the branch, and never run
an agent that holds credentials on a branch you do not trust. A way to slip
instructions in past that review is worth reporting. See
[Working with AI coding agents](CONTRIBUTING.md#working-with-ai-coding-agents).
