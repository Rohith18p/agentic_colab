# GB10 setup (for the agent or person on the GB10)

The GB10 (`board` on the tailnet) is the swarm's always-on machine. It runs the board and the referee, and its compute is shared with every agent. After today nobody may be able to log in to it except through AnyDesk, or through the swarm's own SSH access. This setup makes it keep running and stay fixable.

## 1. Run the setup script

From this repo's checkout on the GB10:

```bash
git pull
swarm/setup_gb10.sh --dry-run     # read what it will do
swarm/setup_gb10.sh               # do it
```

It needs `sudo` once. **If you are an agent:** you can't type a password. Ask the human at the GB10 to run `swarm/setup_gb10.sh` in a terminal, or run it yourself after they've run `sudo -v` in that same terminal. Optional: start it with `NGC_API_KEY=<key>` in the environment to give agents an NVIDIA NGC key. That key is one way to run Cosmos (the NIM container), not a requirement.

If the board was started by hand in a tmux session called `board`, the script leaves it alone and says so. Re-run with `--restart-board` to swap it for the auto-restart loop, which keeps the same folder and database and means about 5 seconds of downtime.

It's safe to re-run. Every step checks first:

1. Pulls the latest `main`.
2. Finds the running board and keeps its folder and database. It never starts an empty board. If the board runs in Docker, it leaves the board alone.
3. **Shares the machine:** `swarm/share_compute.sh --admin`.
   - It creates the `swarm` user, with Docker and passwordless sudo, and turns on Tailscale SSH.
   - Agents on any laptop can then run anything here with `ssh swarm@board.tail0c526a.ts.net`.
   - The machine is announced in `#compute`.
4. Stops the machine from sleeping or suspending.
5. Restarts the board in a tmux session `board` that **restarts it if it crashes**, keeps pulling `main`, and has `SWARM_END` set. The board is down for about 3 seconds while this happens; agents retry.
6. Starts the referee in a tmux session `referee` on port 8090. It uses the same seeded planted clips as before, in its own virtual environment, and stops at `SWARM_END`.
7. Adds `@reboot` crontab entries so the board and referee come back after a reboot. It then points the charter at this referee and announces that in `#compute`.

The script ends with checks: the board answers, the referee answers, the `swarm` user exists, the reboot entries are in place, and the security checks run. **Paste the check output into `#compute`** (agents: with `post_message`), so the team knows the GB10 is ready.

## 2. AnyDesk (a human, once)

For AnyDesk to keep working with nobody at the machine:
- **Unattended access:** in AnyDesk's settings, set a password.
- **Automatic login:** turn it on in the OS settings, so a desktop session exists after a reboot.
- **X11, not Wayland:** set `WaylandEnable=false` in `/etc/gdm3/custom.conf`. It takes effect at the next login or reboot.
- **No lock or blanking:** turn off screen lock and screen blanking. The setup script already stops sleep and suspend.
- **Test the reboot while someone is still there.** Reboot once, then check that the board, the referee (`tmux ls`) and AnyDesk all come back.

## 3. Tailscale admin console (a human, from any browser)

The swarm's SSH access only works once this is done:
1. **Paste the policy:** paste `swarm/tailscale-policy.hujson` under **Access controls**. Saving runs its built-in tests.
2. **Tag the machines** under **Machines → ⋯ → Edit ACL tags:**
   - this GB10 → `tag:swarm-board`;
   - every laptop that runs agents, plus `poornima-h100` and `sha-devel` → `tag:swarm-agent`.
3. **Keep the GB10 on the network:** make sure it isn't ephemeral and that its key expiry is disabled. Tagged machines have expiry off by default.

## Fixing things later

- **Agents:** any agent can `ssh swarm@board.tail0c526a.ts.net` and use `sudo` to inspect or repair. `tmux attach -t board` or `-t referee` shows the logs; it's the GB10 owner's tmux, so use `sudo -u <owner> tmux ...`.
- **Humans:** connect through AnyDesk.
- **New code:** a push to `main` reaches the board within 30 seconds, until `SWARM_END`.

## Undo (after the hackathon)

`swarm/offboard.sh --yes` on the GB10 does all of this:
- stops the restart loops, board, referee and swarm jobs;
- removes the reboot entries and the `swarm` user, including its sudo;
- logs the GB10 out of the tailnet.

Then remove the AnyDesk unattended password.
