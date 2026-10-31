# Making the workspace reachable for teammates

These are notes only; nothing here has been set up. Remember the current version has **no authentication**: anyone who can reach the port can read and change everything. Prefer private networks (a tailnet or VPN) over the public internet.

## Option 1: Tailscale (recommended for now)

Everyone joins the same tailnet, and the machine running the server is reachable at its tailnet name.

1. Install Tailscale on the server machine and on each teammate's PC, and log in to the same tailnet. Alternatively, use [node sharing](https://tailscale.com/kb/1084/sharing) to share just the server machine with people outside your tailnet.
2. Start the server listening on all interfaces:
   ```bash
   HOST=0.0.0.0 make run
   # or: docker compose up   (already listens on 0.0.0.0:8000)
   ```
3. Teammates open `http://<machine-name>.<tailnet>.ts.net:8000` (or `http://100.x.y.z:8000`) and point runners at it:
   ```bash
   WORKSPACE_URL=http://<machine-name>.<tailnet>.ts.net:8000 AGENT_NAME=bob-agent python -m runner
   ```
4. Optional: turn on Host-header checking for `/mcp` with
   `MCP_ALLOWED_HOSTS="<machine-name>.<tailnet>.ts.net:*,100.x.y.z:*,localhost:*,127.0.0.1:*"`.
5. Optional: `tailscale serve` can put HTTPS in front of port 8000 inside the tailnet. Check the current Tailscale docs for the exact syntax. Don't use `tailscale funnel`, which exposes the server to the whole internet, until there are logins.
6. Use Tailscale ACLs to limit which users or devices can reach port 8000.

## Option 2: Same LAN

Run with `HOST=0.0.0.0 make run`, find the machine's LAN IP, and share `http://<ip>:8000`. This works only on trusted networks (home or office). You may need to allow port 8000 through the OS firewall.

## Option 3: A small VM later

When there are logins, the app can run on a small VM: `docker compose up -d`, behind a reverse proxy (Caddy or nginx) with TLS. Before that happens, the checklist is:

- Add authentication (agent tokens, human logins). The `X-Display-Name` header and cookie are not security.
- Move to Postgres (`DATABASE_URL=postgresql+psycopg://...`) and add migrations.
- Keep a single app process, or move the live-update notifier and the lease sweeper to something shared (e.g. Postgres LISTEN/NOTIFY and a single scheduler).
- Make sure the proxy doesn't buffer `/api/stream` (SSE). The app already sends `X-Accel-Buffering: no` for nginx.

## Things to remember

- Back up the SQLite file (`data/workspace.db`, or the `workspace-data` Docker volume). Stop the server or use `sqlite3 .backup` so WAL contents are included.
- The charter warning is there for a reason: no API keys or passwords anywhere in the workspace.
