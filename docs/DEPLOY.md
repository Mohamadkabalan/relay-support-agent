# Deploying the demo

The public demo runs on one VM with Docker Compose and Caddy ([ADR-0011](adr/0011-one-vm-with-compose-and-caddy.md)). This guide goes from an empty Hetzner Cloud project to a working demo, then covers updates, the nightly reset, logs, backups and rolling back.

What runs where:

| Piece | Where |
|---|---|
| `compose.prod.yaml` | The stack: Postgres, Redis, Phoenix, the migrate job, the API, the MCP server, the web app, Caddy |
| `deploy/Caddyfile` | HTTPS for your domain, security headers, `/api/*` to the API |
| `deploy/env.prod.example` | Every setting the stack needs; your copy lives on the VM as `/opt/relay/.env` |
| `deploy/deploy.sh` | Copies the files above to the VM, starts a version, smoke-checks it |
| `deploy/systemd/` | The nightly reset at 04:00 UTC |
| `.github/workflows/release.yml` | Builds and pushes the images when you push a `v*` tag |

## 1. What you need

- A Hetzner Cloud account (any VM provider works the same way), and a domain whose DNS you control.
- The GitHub repository, with Actions enabled. Releases push images to GHCR under your account.
- An Anthropic API key. Create a **dedicated Claude Console workspace** for the demo and give it a monthly spend limit: it is the backstop behind the app's own daily cap.
- On your machine: `ssh`, `scp` and `curl`.

## 2. Provision the VM

1. Create a VM with **4 GB of RAM**, Ubuntu LTS, and your SSH key. x86 or Arm both work only if the images are built for that architecture; the release workflow builds `linux/amd64`, so pick an x86 type.
2. Attach a **firewall**: inbound TCP 22 from your own address only, TCP 80 and 443 and UDP 443 from anywhere. Nothing else.
3. Point DNS at it: an `A` record (and `AAAA` for IPv6) for your domain, say `relay.example.com`. Wait until `dig +short relay.example.com` returns the VM's address; Caddy needs that to get a certificate.
4. Install Docker Engine with the Compose plugin, following Docker's guide for Ubuntu (docs.docker.com/engine/install/ubuntu). Check with `docker compose version`.
5. Create the deploy user and directory:

   ```bash
   sudo adduser --disabled-password --gecos "" relay
   sudo usermod -aG docker relay
   sudo mkdir -p /opt/relay && sudo chown relay:relay /opt/relay
   sudo mkdir -p /home/relay/.ssh && sudo cp ~/.ssh/authorized_keys /home/relay/.ssh/
   sudo chown -R relay:relay /home/relay/.ssh
   ```

   Membership in the `docker` group is root-equivalent on that machine; keep SSH access to your own key.

## 3. Publish the images

Push a version tag; the Release workflow builds and pushes both images:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

The images are `ghcr.io/<owner>/relay-backend:1.0.0` and `ghcr.io/<owner>/relay-web:1.0.0` (owner in lowercase). New GHCR packages are private. Either make both packages public (package settings on GitHub), or log the VM in once with a token that has only `read:packages`:

```bash
ssh relay@relay.example.com 'docker login ghcr.io -u <github-user>'   # paste the token
```

## 4. Configure

Copy `deploy/env.prod.example` to the VM as `/opt/relay/.env` and fill it in:

```bash
scp deploy/env.prod.example relay@relay.example.com:/opt/relay/.env
ssh relay@relay.example.com 'chmod 600 /opt/relay/.env && nano /opt/relay/.env'
```

- `RELAY_DOMAIN`, `ACME_EMAIL`, `RELAY_IMAGE_PREFIX` (`ghcr.io/<owner>`), `ANTHROPIC_API_KEY`.
- Four secrets, each from `openssl rand -hex 32`: `POSTGRES_PASSWORD`, `RELAY_APP_DB_PASSWORD`, `RELAY_MCP_JWT_SECRET`, `RELAY_SESSION_SECRET`. The API refuses to start in production with a value that contains `dev-only`.
- `RELAY_DAILY_SPEND_CAP_USD` (default 20): when the next model call's worst case would pass it, the chat says the demo is paused until 00:00 UTC and makes no call.
- Optionally a real operator login besides the demo one: `docker run --rm ghcr.io/<owner>/relay-backend:1.0.0 relay operator hash-password`, then `RELAY_OPERATOR_PASSWORD_HASH='...'` in single quotes.

Compose refuses to start while any required value is missing, and names it.

## 5. First deploy

```bash
deploy/deploy.sh relay@relay.example.com 1.0.0
```

It copies `compose.prod.yaml`, the Caddyfile, the Postgres init script and the systemd units; sets `RELAY_VERSION`; pulls the images; starts the stack and waits until every service is healthy (the migrate job runs first: migrations, then the `relay_app` role and its grants); and checks `https://<domain>/api/readyz`. The first start also waits for Caddy to get the certificate.

Then seed the store once, and install the nightly reset:

```bash
ssh relay@relay.example.com
cd /opt/relay
docker compose -f compose.prod.yaml run --rm migrate relay seed      # about a minute; checks itself
sudo cp deploy/systemd/relay-demo-reset.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now relay-demo-reset.timer
systemctl list-timers relay-demo-reset.timer                          # the next run at 04:00 UTC
```

## 6. Check the demo (the deploy checklist)

Phase 8's acceptance test. Do it after every first deploy, and after any release that touches the agent, the API or the console.

- [ ] `https://<domain>` loads over HTTPS, with the demo banner. `http://<domain>` redirects to HTTPS.
- [ ] `curl -sI https://<domain>/ | grep -i strict-transport-security` prints the HSTS header.
- [ ] Pick **Dana**, open the chat, and say the headphones from her order arrived with a cracked headband.
- [ ] The agent finds the order, describes the $349.99 refund and asks you to confirm; you say yes; it says a team member must review it, with a reference number.
- [ ] In a second browser (or a private window), open `https://<domain>/console`, sign in as the demo operator, open **Approvals**, and approve the request with a note.
- [ ] Back in the chat, without reloading, the agent tells Dana the refund was approved.
- [ ] In the console, the conversation's replay shows each turn with its model calls, tool calls and cost.
- [ ] With the tunnel from section 8 open, the replay's trace link opens the turn in Phoenix.
- [ ] Ask for a human in a new conversation: it appears under **Escalations**.

## 7. Updates

```bash
git tag v1.1.0 && git push origin v1.1.0       # wait for the Release workflow
deploy/deploy.sh relay@relay.example.com 1.1.0
```

`up` restarts only the containers whose image or settings changed. Open chats lose their event stream for a moment and reconnect; nothing is lost, because events are stored and replayed after a reconnect.

## 8. Operating it

**Phoenix** listens on the VM's loopback only. Open a tunnel, then browse http://localhost:6006 (the console's trace links point there):

```bash
ssh -N -L 6006:127.0.0.1:6006 relay@relay.example.com
```

**Logs** (JSON, rotated at 10 MB x 5 per container):

```bash
docker compose -f compose.prod.yaml logs --tail=200 --follow api
docker compose -f compose.prod.yaml ps
```

**The nightly reset** wipes every customer, order, conversation, approval and audit row, seeds the store again relative to the current date, and signs everyone out. Spend counters are kept, so it can't reopen the daily cap. It takes about a minute, during which requests wait. Run it by hand with `sudo systemctl start relay-demo-reset.service`, and read its output with `journalctl -u relay-demo-reset.service`. Outside demo mode (`RELAY_DEMO_MODE=false`) the command refuses to run.

**Backups.** The demo resets nightly, so there is little to keep. Before a release with a migration, take one anyway:

```bash
docker compose -f compose.prod.yaml exec -T postgres pg_dump -U relay -Fc relay > relay-$(date +%F).dump
```

## 9. Rolling back

Deploy the previous version: `deploy/deploy.sh relay@relay.example.com 1.0.0`.

Migrations only move forward. If the release you're leaving added one, the old code may not run against the new schema. Then restore the backup taken before the release:

```bash
docker compose -f compose.prod.yaml stop api mcp web
docker compose -f compose.prod.yaml exec -T postgres pg_restore -U relay -d relay --clean --if-exists < relay-YYYY-MM-DD.dump
deploy/deploy.sh relay@relay.example.com 1.0.0     # from your machine
```

For the demo, `relay demo reset --yes` on the old version is an alternative: it rebuilds everything from the seed.

## 10. When something is wrong

| Symptom | Look at |
|---|---|
| `deploy.sh` stops with "required variable ... is missing" | The named key in `/opt/relay/.env` |
| Caddy keeps failing to get a certificate | DNS (`dig`), and that ports 80 and 443 are open in the firewall |
| `migrate` exits non-zero | `docker compose -f compose.prod.yaml logs migrate` |
| The API is unhealthy | `logs api`; a production check such as a `dev-only` secret or `RELAY_COOKIE_SECURE` off stops it at startup |
| The chat says the demo is paused | The daily spend cap; it reopens at 00:00 UTC |
| `pull` is denied | GHCR visibility, or `docker login ghcr.io` on the VM (section 3) |
