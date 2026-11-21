# Running the card engine with Docker (local or EC2)

Three containers, one command.

| Container | Image | What it does | Reachable from |
|---|---|---|---|
| `db` | postgres:18 | the database (to be moved to a managed one later: change `CD_DATABASE_URL`) | backend only |
| `backend` | `Dockerfile` | **the engine**: API, 2 workers, scheduler; OCR tools; Claude CLI | frontend only (no ports published) |
| `frontend` | `deploy/frontend/Dockerfile` | a thin test page (static, no data or secrets) + nginx forwarding `/api` and `/oauth` | the outside (80, or 443 with HTTPS) |

**Networks.** `data` (internal: no internet, nothing published) joins only db and backend, so the
frontend can't reach the database. `app` (internal) joins frontend and backend. `public` gives the
backend outbound access (Gmail, Anthropic) and the frontend its published port.

**Developer panel.** Only the Gmail addresses in `CD_ADMIN_EMAILS` see it; for everyone else it isn't
shown and every `/api/dev/*` endpoint answers 404.

**Volumes.**
- `pgdata`: the database.
- `cdhome` (`/data` in the backend): the **vault master key**, which unlocks stored data (losing it
  means every user reconnects Gmail), plus the study set and analysis files. Back it up.
- Google's OAuth client JSON is mounted read-only from the host (`GOOGLE_CLIENT_FILE`), never baked
  into the image.

## Start

```bash
cp .env.example .env
```

Edit `.env` (password, public URL, Google client file, optional Haiku settings), then:

```bash
docker compose up -d --build
```

```bash
docker compose logs backend | grep "API:"
```

That last line prints the link to open, with the team's `?t=` access token. Database migrations run
by themselves when the backend starts.

## On EC2

1. Install Docker and the compose plugin, clone the repo, and put the OAuth client JSON on the host.
2. `.env`:
   - `CD_PUBLIC_URL=https://<your-domain>`
   - `HTTP_PORT=80`
   - a long random `POSTGRES_PASSWORD`
   - for Haiku: `CD_LLM_FALLBACK=cli`, `CD_LLM_ACCOUNTS=<ids>`, `CLAUDE_CODE_OAUTH_TOKEN=<from claude setup-token>`
   - leave `CARDDISCOVERY_DEV` unset, so `?user=` sign-in stays off and each person gets their own account.
3. Google Cloud console: a **Web application** OAuth client with
   `https://<your-domain>/oauth/callback` as an authorised redirect URI, and each team member added as a
   test user. A *Desktop* client only allows `127.0.0.1` / `localhost`, which is fine locally but not on EC2.
4. HTTPS, either way:
   - **Load balancer / Cloudflare in front** (simplest): TLS ends there and nginx stays on port 80.
   - **On nginx**: get certificates with certbot on the host, copy `deploy/nginx/https.conf.example` to
     `deploy/nginx/default.conf`, uncomment the 443 port and the certs volume in `docker-compose.yml`.
5. Security group: open 80/443 only. Postgres and the backend are never published.

## Everyday

| Task | Command |
|---|---|
| Logs | `docker compose logs -f backend` |
| Update after `git pull` | `docker compose up -d --build backend` |
| Back up the database | `docker compose exec db pg_dump -U cd carddiscovery > backup.sql` |
| Stop everything | `docker compose down` (volumes are kept; `down -v` deletes all data) |
