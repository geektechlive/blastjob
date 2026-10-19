# Using the cockpit from another device

**What this covers:** Reaching the cockpit from a phone or tablet on your home network, and what that exposes. For anyone deciding whether to turn LAN access on. It is always your decision; an assistant helping you set up should not make it for you.

## Before you turn it on

The cockpit has no login. By default it listens on `127.0.0.1`, so only the machine it runs on can reach it. If you let it listen on your network, anyone on that network can open it, read your whole job search and your work history, and start actions that spend your language model budget.

Think about who is on that network: guests, smart devices, a shared office. If you would not hand them your resume, leave LAN access off.

blastjob still has some protection when LAN access is on:

- It answers only requests addressed to a local or private IP address. A request that names the machine by a public domain is refused. This is what stops a web page you visit from reaching it by DNS tricks.
- Every change needs a token from the page and a local origin, so a web page on the internet cannot push buttons for you.
- Responses forbid being shown inside another site's frame.

None of these identify who is asking. They do not protect you from someone on your own network.

Because of the first rule, open the cockpit by the machine's IP address (for example `http://192.168.1.20:8765`), not by a name such as `my-laptop.local`. A name is refused.

## Turning on LAN access

Set the address and restart the cockpit:

```bash
uv run blastjob setup set web.host 0.0.0.0
```

The command prints a warning that the cockpit has no login; that is expected. If the cockpit runs as a service, restart it as shown in [running the services](services.md#the-cockpit-service). For a single launch without saving the setting, pass the address on the command line instead:

```bash
uv run blastjob-web --host 0.0.0.0
```

While it is bound to the network, the cockpit logs a warning each time it starts. To turn LAN access off again, set `web.host` back to `127.0.0.1` and restart.

On a Mac the application firewall may drop the traffic. The venv's Python is not the one the firewall knows, and the checkout's `.venv` path is long, so the helper script works it out for you:

```bash
sudo sh scripts/allow-firewall.sh
```

It adds the cockpit's Python to the firewall's allowed list. Then check from the Mac that the cockpit really answers on your network address:

```bash
sh scripts/verify-lan-access.sh
sh scripts/verify-lan-access.sh my-other-machine    # also probe from a machine you can ssh to
```

The check confirms that the cockpit listens on all interfaces, that it answers on the loopback and network addresses, that a button press from a local address is accepted and one from a public origin is refused. The check from another machine is the only one that proves another device can connect, so give it an ssh host if you can. It reads the port from the `PORT` environment variable, default 8765. If it fails only the reachability checks, it prints the firewall commands to run.

## What stays loopback-only

Some actions only make sense on the machine itself. **Reveal in Finder**, on a job's packet, opens a Finder window on the Mac. It works only when the click comes from the Mac itself, not from another device on the network.
