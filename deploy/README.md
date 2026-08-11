# Deploying

Everything needed to run this on a box, plus the operations you will actually perform on it. The
artifacts here are complete and exercised — CI stands the whole stack up on every push and runs the
demo against it — but **no machine has been provisioned**. That is a deliberate stopping point, not
an unfinished one: the images, the compose topology, the nginx config and these scripts are the work,
and renting an instance is a decision with a monthly bill attached.

## The shape

```
browser ──▶ nginx (web container)
             ├── /            the built SPA, with a try_files fallback
             ├── /api/  ──┐
             ├── /ws/   ──┼──▶ gunicorn + uvicorn workers (app container)
             └── /static ─┘         │
                                    ├──▶ Postgres
                                    └──▶ Redis ── cache (db 0)
                                                ├─ Celery broker (db 1) ──▶ worker, beat
                                                └─ channel layer (db 2)
```

One origin serves all of it. That is not a preference — the client hardcodes `BASE = "/api/v1"` and
builds its socket URL from `window.location.host` (ADR-0030), so a reverse proxy on a single origin
is the only arrangement it runs in. The upside is that there is no CORS layer anywhere in the system.

`app`, `worker`, `beat` and the one-shot `migrate` are all the **same image** with different commands
(ADR-0033). They run the same code and must not be able to drift apart in their dependencies.

## First deploy

```sh
./deploy/bootstrap.sh                      # docker, swap, ufw, certbot, /srv/banking
cp deploy/.env.example deploy/.env         # then fill it in — see below
sudo certbot certonly --webroot -w /var/www/certbot -d bank.example.com
./deploy/deploy.sh <git-sha>
```

### Turning the deploy workflow on

Two settings, in this order, and the first is not optional:

1. **Settings → Environments → production → Required reviewers.** `environment: production` in the
   workflow does *not* create a protected environment — GitHub creates it unprotected on first use,
   and the job runs unattended. This is the approval gate; the workflow cannot assert it.
2. `gh secret set SSH_HOST --env production` (and `SSH_USER`, `SSH_KEY`), then
   `gh variable set DEPLOY_ENABLED --body true`.

Until step 2, the deploy job skips rather than failing, so `main` is not permanently red while the
box does not exist.

Sizing: **`t3.small` (2 GB), not `t3.micro`.** Steady state is roughly Postgres 200 MB + Redis 30 MB
+ two gunicorn workers 300 MB + Celery 150 MB + Beat 100 MB + nginx 10 MB ≈ 800 MB. The 2 GB swapfile
`bootstrap.sh` adds is not for running the app; it is for the spikes — a `pg_dump` alongside
everything else, or an image layer decompressing while all six containers are up. Without it those
meet the OOM killer, which picks the largest process, which is Postgres.

### The two variables with no defaults

`prod.py` refuses to boot without `DJANGO_SECRET_KEY` or a non-empty `FIELD_ENCRYPTION_KEYS`, and
that refusal is the point (ADR-0027): booting with a key an attacker could read from the repository
would leave the column *looking* encrypted.

**Back the keyring up somewhere that is not this box.** Losing it means losing every account number
and TOTP secret in the database — the ciphertext survives and nothing can read it.

### Three settings that will waste an hour if you skip them

| Variable | Why |
|---|---|
| `DB_SSLMODE=disable` | `prod.py` defaults to `require`, which is right when the database is on another host. Here it is a container on this host's private bridge with no published port, and `postgres:16-alpine` ships no certificate, so `require` simply fails to connect. Set it back to `require` the day the database leaves this box. |
| `CSRF_TRUSTED_ORIGINS` | The SPA uses bearer tokens and does not care. **Django admin login 403s without it** the moment it is behind a proxy. Scheme included, no path. |
| `DJANGO_ALLOWED_HOSTS` includes `localhost` | The app container's healthcheck requests `http://127.0.0.1:8000/api/v1/ready/`, and Django rejects a Host it does not recognise. Safe: the container publishes no ports. |

## Routine operations

```sh
./deploy/deploy.sh <sha>          # ship, or roll back — same command, different tag
docker compose -f deploy/compose.yml --env-file deploy/.env logs -f app
docker compose -f deploy/compose.yml --env-file deploy/.env ps
```

**Rollback is a deploy of an older SHA.** Compose pins `${IMAGE_TAG}` and never `latest`, which is
exactly what makes that true: with a floating tag, "roll back" and "rebuild" become the same command
and neither is reproducible. The previous images stay on disk until a *healthy* release prunes them,
so the tag you want is in `docker images`.

There is a gap of a few seconds during a restart when nginx has no backend. That is not hidden —
nginx answers it with the ADR-0006 error envelope as a 503, so the client reports an outage rather
than failing to parse an HTML error page. A single box does not get zero-downtime deploys, and
pretending otherwise would mean a second box.

### Certificates

Certbot runs on the **host**, not as a container (ADR-0040). `/etc/letsencrypt` is bind-mounted
read-only into `web`, so renewal keeps working even when the stack is down — which is precisely when
an ACME sidecar would not. Renewal needs one hook so nginx picks up the new file:

```sh
echo 'docker compose -f /srv/banking/deploy/compose.yml exec web nginx -s reload' \
  | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

**On HSTS preload.** `prod.py` sets `SECURE_HSTS_PRELOAD = True`, which only adds a header — that is
harmless and correct. *Submitting the domain to the preload list* is the commitment, and with
`includeSubDomains` it makes every sibling subdomain HTTPS-only in shipped browsers for months, with
removal taking longer than that. Use a dedicated domain, send the header, and do not submit.

### Backups

```sh
(crontab -l 2>/dev/null; echo '0 3 * * * /srv/banking/deploy/backup.sh >> /var/log/banking-backup.log 2>&1') | crontab -
```

`backup.sh` dumps Postgres and tars `media/`, encrypts both with `BACKUP_PASSPHRASE`, and ships them
to `BACKUP_S3_URI`. **The media half is why ADR-0039 could decline S3 storage**: keeping statement
PDFs on a local volume is only defensible if they leave the box on a schedule. Without this script,
"we chose FileSystemStorage" would just mean "we chose one disk".

**Run `./deploy/restore.sh` once, this week, against a real backup.** Every way a backup fails is
silent — a dump truncated by a full disk, a passphrase nobody wrote down, a `pg_dump` that has been
producing zero bytes since a container was renamed. It restores into a scratch database and prints
row counts to compare against production; it does not touch the live one, because recovering for real
should be a decision somebody makes at a keyboard.

### Re-seeding

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env exec app \
  python manage.py seed_demo --seed 1
```

**There is no `--reset`, and there cannot be.** `AuditEvent` is append-only — a Postgres trigger
refuses `UPDATE` and `DELETE` — and `AuditEvent.actor` is `PROTECT`, so a customer who has done
anything cannot be deleted and their audit rows cannot even have the actor nulled. Three guarantees
meeting, all working as designed. Re-seeding therefore means an empty database:

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env down -v
./deploy/deploy.sh <sha>
```

### The admin

`/admin/` is IP-allowlisted in `deploy/nginx/admin-allowlist.conf` and closed by default. It is the
weakest surface on the box: Django admin is session auth with a password, and the TOTP enforced on
`/api/v1/auth/` does not apply to it — a stolen superuser password is the entire control, on a form
that can read every account in the ledger. Widen it while demoing, then narrow it again:

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env exec web nginx -s reload
```

## What is deliberately not here

No Kubernetes, Terraform, autoscaling or managed database — one box, and the compose file is the
whole topology. No Sentry, metrics or tracing: there are health and readiness probes and structured
logs with request ids (ADR-0028), and the honest next step is a log shipper, not an agent. No
blue/green. No S3 for media (ADR-0039) — the trigger that would invert that is a second app host, and
the change is one entry in `STORAGES`.
