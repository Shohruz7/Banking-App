# Deploying

Everything needed to run this on a box, plus the operations you will actually perform on it. CI
stands the whole stack up on every push and runs the demo against it, so the images, the compose
topology, the nginx config and these scripts are exercised rather than asserted.

## The shape

```
browser ──▶ nginx (web container)
             ├── /            the built SPA, with a try_files fallback
             ├── /api/  ──┐    upstream app
             ├── /ws/   ──┼──▶ ├─ app_blue  10.99.0.11:8000   gunicorn + uvicorn workers
             └── /static ─┘    └─ app_green 10.99.0.12:8000   (rolled one at a time)
                                    │
                                    ├──▶ Postgres
                                    └──▶ Redis ── cache (db 0)
                                                ├─ Celery broker (db 1) ──▶ worker, beat
                                                └─ channel layer (db 2)
```

One origin serves all of it, and that is not a preference. The client hardcodes `BASE = "/api/v1"` and
builds its socket URL from `window.location.host` (ADR-0030), so a reverse proxy on a single origin
is the only arrangement it runs in. The upside is that there is no CORS layer anywhere in the system.

`app`, `worker`, `beat` and the one-shot `migrate` are all the **same image** with different commands
(ADR-0033). They run the same code and must not be able to drift apart in their dependencies.

## First deploy

```sh
./deploy/bootstrap.sh                      # docker, swap, ufw, certbot, /srv/banking
cp deploy/.env.example deploy/.env         # then fill it in; see below
sudo certbot certonly --webroot -w /var/www/certbot -d bank.example.com
./deploy/deploy.sh <git-sha>
```

### Turning the deploy workflow on

Two settings, in this order, and the first is not optional:

1. **Settings → Environments → production → Required reviewers.** `environment: production` in the
   workflow does *not* create a protected environment. GitHub creates it unprotected on first
   use, and the job runs unattended. This is the approval gate; the workflow cannot assert it.
2. `gh secret set SSH_HOST --env production` (and `SSH_USER`, `SSH_KEY`), then
   `gh variable set DEPLOY_ENABLED --body true`.

Until step 2, the deploy job skips rather than failing, so `main` is not permanently red while the
box does not exist.

Sizing: **`t3.small` (2 GB), not `t3.micro`.** Steady state is roughly Postgres 200 MB + Redis 30 MB
+ **two app replicas at ~300 MB each** + Celery 150 MB + Beat 100 MB + nginx 10 MB ≈ 1.1 GB. The
second replica is the cost of rolling deploys (ADR-0043); `GUNICORN_CMD_ARGS=--workers 1` in `.env`
halves it with no rebuild if the box ever needs the room. The 2 GB swapfile
`bootstrap.sh` adds is not for running the app; it is for the spikes: a `pg_dump` alongside
everything else, or an image layer decompressing while all six containers are up. Without it those
meet the OOM killer, which picks the largest process, which is Postgres.

### The two variables with no defaults

`prod.py` refuses to boot without `DJANGO_SECRET_KEY` or a non-empty `FIELD_ENCRYPTION_KEYS`, and
that refusal is the point (ADR-0027): booting with a key an attacker could read from the repository
would leave the column *looking* encrypted.

**Back the keyring up somewhere that is not this box.** Losing it means losing every account number
and TOTP secret in the database. The ciphertext survives and nothing can read it.

### Three settings that will waste an hour if you skip them

| Variable | Why |
|---|---|
| `DB_SSLMODE=disable` | `prod.py` defaults to `require`, which is right when the database is on another host. Here it is a container on this host's private bridge with no published port, and `postgres:16-alpine` ships no certificate, so `require` simply fails to connect. Set it back to `require` the day the database leaves this box. |
| `CSRF_TRUSTED_ORIGINS` | The SPA uses bearer tokens and does not care. **Django admin login 403s without it** the moment it is behind a proxy. Scheme included, no path. |
| `DJANGO_ALLOWED_HOSTS` includes `localhost` and `127.0.0.1` | Each app replica's healthcheck requests `http://127.0.0.1:8000/api/v1/ready/`, and Django rejects a Host it does not recognise. Safe: the container publishes no ports. **Do not add an nginx upstream name here.** If requests arrive with `Host: app`, `proxy-headers.conf` is not being included in the location that served them. Adding the name papers over that and takes `X-Forwarded-Proto`, `X-Forwarded-For` and `X-Request-ID` down with it. |

### The forwarded headers, and what now checks them

nginx inherits `proxy_set_header` and `add_header` into a location only when that location declares
none of its own. Every proxied location declares one, so for nine weeks every proxied location was
sending none of the forwarded headers, and nothing said so: not the parse, not the runtime, not the
access log. Four things now assert what used to be assumed.

| Check | Where | What it would catch |
|---|---|---|
| Every `location` with `proxy_pass` carries the include | `nginx_header_inheritance.py`, in the fast CI job | The original bug, on the commit that introduced it. Also covers the `add_header` half, which served the SPA's own HTML with no CSP. |
| `X-Request-ID` round-trips and reaches the audit row | stack job | The header not arriving, or arriving and being ignored |
| A forged `X-Forwarded-For` does not move the throttle key | stack job | `NUM_PROXIES` wrong or unset, which makes every rate limit in the system decorative |
| A malformed `X-Forwarded-For` is not a 500 | stack job | Client-controlled text reaching `AuditEvent.ip` |

The last one was a live bug found while writing the third. The leftmost `X-Forwarded-For` entry is
whatever the caller typed, and it went straight into a `GenericIPAddressField`, so
`X-Forwarded-For: not-an-ip` was a 500 on every audited endpoint. Login and registration are
audited and take no credentials, so it needed no account and one header. A claim that is not an
address is now discarded and the row records what nginx observed instead.

**`X-Forwarded-Proto` is covered in three halves rather than end to end, and the gap is real.**
Proving it on a live request means turning `SECURE_SSL_REDIRECT` on, which makes every request
redirect, including each replica's own healthcheck, so the stack never reports healthy and the job
cannot get far enough to assert anything. What is asserted instead:

| | |
|---|---|
| nginx sends it | `nginx_header_inheritance.py` |
| Django honours it when sent | `tests/test_audit_context.py`, against `SECURE_PROXY_SSL_HEADER` |
| the deployment asks Django to | a CI step loading prod settings, asserting the header and the redirect are both on |

Nothing joins them on one request. If that header stops arriving, these three still pass and the
symptom in production is an infinite redirect the moment TLS terminates upstream.

## Routine operations

```sh
./deploy/deploy.sh <sha> [web-tag]   # ship, or roll back: same command, different tag
docker compose -f deploy/compose.yml --env-file deploy/.env logs -f app_blue app_green
docker compose -f deploy/compose.yml --env-file deploy/.env ps
```

**Rollback is a deploy of an older SHA.** Compose pins `${IMAGE_TAG}` and never `latest`, which is
exactly what makes that true: with a floating tag, "roll back" and "rebuild" become the same command
and neither is reproducible. The previous images stay on disk until a *healthy* release prunes them,
so the tag you want is in `docker images`.

**The API and the WebSocket roll without dropping a request. The edge still restarts when the
bundle changes.** Two app replicas, `app_blue` and `app_green`, are replaced one at a time behind a
statically-addressed nginx upstream (ADR-0043); CI proves it by holding one replica down and
asserting traffic still succeeds. What does not roll is `web`, which owns port 80, so it carries
its own content-derived tag and a backend-only release leaves it untouched. A frontend release
still blips for about a second.

Two things follow that are easy to miss:

- **Migrations must be expand-only.** `deploy.sh` migrates before it rolls either replica, so the
  previous release's code serves against the new schema for the length of the deploy. Add in one
  release, remove in a later one. A CI job refuses the destructive operations without an explicit
  `EXPAND-CONTRACT-EXEMPT:` marker.
- **Never put `nginx -s reload` in the deploy path.** The upstream is two literal addresses for
  exactly this reason: with hostnames, a reload while a peer is down fails to parse, never sends
  SIGHUP, and leaves nginx silently serving stale addresses.
- **A change to the network block needs `down` before `up`, and `deploy.sh` will not do it.**
  Editing `subnet`, `ip_range` or `gateway` and then running a normal release reconfigures the
  network under the containers already attached to it, and they come back with their service
  aliases dropped. DNS then fails for every service name while the addresses still route, so the
  symptom is `migrate` timing out in `pool.getconn()` against a Postgres that is up, healthy and
  two addresses away. Nothing in the output says "network". Schedule that edit as a short full
  restart instead:

  ```sh
  docker compose -f deploy/compose.yml --env-file deploy/.env down     # keeps volumes
  ./deploy/deploy.sh <sha>
  ```

With both replicas down there is still no backend, and that is not hidden. nginx answers with the
ADR-0006 error envelope as a 503, so the client reports an outage rather than failing to parse an
HTML error page. None of this is high availability: one box, one Postgres, one Redis. It buys the
outage that happens on a schedule, not the one that happens by surprise.

### Certificates

Certbot runs on the **host**, not as a container (ADR-0040). `/etc/letsencrypt` is bind-mounted
read-only into `web`, so renewal keeps working even when the stack is down, which is precisely when
an ACME sidecar would not. Renewal needs one hook so nginx picks up the new file:

```sh
echo 'docker compose -f /srv/banking/deploy/compose.yml exec web nginx -s reload' \
  | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

**On HSTS preload.** `prod.py` sets `SECURE_HSTS_PRELOAD = True`, which only adds a header, and that is
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

`restore.sh` restores into a **scratch** database, prints row counts, and then runs
`check_ledger_invariants` against the restored copy. It never touches the live database. Recovering
for real is the same `pg_restore` with `--dbname` pointed at production, and that should be a
decision somebody makes at a keyboard. The invariant check is the part that matters: row counts
prove the dump arrived, not that it arrived *consistent*, and a dump that lands mid-transaction is
damaged in exactly the shape this ledger's invariants describe.

### The drill, run

Rehearsed against the full seeded dataset on the local stack. Both scripts take `ENV_FILE` and
`COMPOSE_OVERRIDE` so the drill can point at the CI-shaped stack `make up` brings up:

```sh
make up && make seed
export BACKUP_PASSPHRASE=...        # any passphrase; this copy is thrown away
env BACKUP_DIR=/tmp/banking-drill ENV_FILE=deploy/.env.ci \
    COMPOSE_OVERRIDE=deploy/compose.ci.yml ./deploy/backup.sh
env ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
    ./deploy/restore.sh /tmp/banking-drill/db-<stamp>.dump.gpg
```

| | |
|---|---|
| Dump size (encrypted) | 2.8 MB |
| Rows recovered | 401 users · 2,427 accounts · 13,011 entries · 27,228 lines · 1,600 orders · 15,005 audit events |
| Row counts vs. source | identical on all six |
| Invariants on the restored copy | hold |
| Wall clock, decrypt → restore → verify | **~1.7 s** |

That number is not an RTO and should not be quoted as one. At this data size the restore is
dominated by process startup: `pg_restore` itself is ~0.3 s, and turning `fsync` back on changes
nothing measurable. What the drill establishes is that the path works end to end and that the
backup is real; the recovery time that would matter on a bad day is dominated by provisioning a
box, not by moving 2.8 MB.

**The first run found two bugs, which is the entire argument for running it.** `gpg` failing left
the plaintext dump, every account number and TOTP secret in the system, sitting in `BACKUP_DIR`
while the script aborted under `set -e`; there is now a `trap` that removes it on any path that is
not a successful encryption. And the log timestamps used `date -uIs`, a GNU extension that prints
an error on BSD date, so the drill could not be rehearsed cleanly on a laptop at all.

### The off-box half, run

The drill above restores from a file that never left the machine, so it proves the dump is sound
and says nothing about the path that makes it a backup. `BACKUP_S3_URI` had never been set on any
run. Rehearsed against MinIO, which speaks the same API, so the only untested thing left is the
bucket itself:

```sh
docker run -d --name banking-minio-drill --network banking_default -p 9000:9000 \
  -e MINIO_ROOT_USER=drill-access-key -e MINIO_ROOT_PASSWORD=drill-secret-key \
  quay.io/minio/minio:latest server /data

export AWS_ACCESS_KEY_ID=drill-access-key AWS_SECRET_ACCESS_KEY=drill-secret-key \
       AWS_DEFAULT_REGION=us-east-1 AWS_ENDPOINT_URL=http://localhost:9000 \
       BACKUP_PASSPHRASE=drill-passphrase-thrown-away
aws s3 mb s3://banking-backups

env BACKUP_DIR=/tmp/banking-s3-drill BACKUP_S3_URI=s3://banking-backups \
    ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml ./deploy/backup.sh

rm -rf /tmp/banking-s3-drill                      # the point: nothing local survives
aws s3 cp s3://banking-backups/ /tmp/restore/ --recursive
env ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
    ./deploy/restore.sh /tmp/restore/db-<stamp>.dump.gpg
```

`backup.sh` needed no change for this. The AWS CLI reads `AWS_ENDPOINT_URL` natively from v2.13,
and `bootstrap.sh` already installs the CLI.

| | |
|---|---|
| Shipped | `db-<stamp>.dump.gpg` 3.9 MB · `media-<stamp>.tar.gz.gpg` 0.9 MB |
| Media archive | 1,699 files, 897 statement PDFs |
| Local copies before restoring | deleted, so the restore could only come from the bucket |
| Rows recovered | 402 users · 2,430 accounts · 24,152 entries · 49,510 lines · 1,600 orders · 26,227 audit events |
| Row counts vs. source | identical on all six |
| Invariants on the restored copy | hold |
| Backup and ship | ~4 s |
| Retrieve and restore and verify | ~2 s |

**Generate statements before running this or the media half proves nothing.** The first attempt
shipped a 208-byte tarball, because the seeded dataset creates no PDFs and `media/` was empty. That
half of the backup is the reason ADR-0039 could decline S3 for storage, so a drill that skips it
tests the less interesting claim. `generate_monthly_statements` for a past period fills it.

One number moves by design. The restored copy ends with one more audit row than the source, because
`check_ledger_invariants` writes a `ledger.reconciled` row wherever it is pointed: "we looked, and
here is what we found". The append-only log recording its own verification is the log working, even
on a scratch copy.

**This run found a third bug, in the step that matters most.** The invariant check died with
`failed to set up container networking: Address already in use`. A one-off `compose run` inherits
the address of the service it runs as, and the two app replicas hold pinned addresses so nginx can
name them (ADR-0043), so with the stack up that address is already taken. The check now runs as the
`migrate` service, which is the same image with the same environment and no pinned address. Row
counts had already printed by then, which is exactly how this would have been missed: the drill
looks like it passed unless you read to the end.

### The deploy drill, run

`deploy.sh` takes the same `ENV_FILE` and `COMPOSE_OVERRIDE` overrides the backup scripts do, plus
`SKIP_PULL` and `SKIP_PRUNE`, so a release can be rehearsed against the stack `make up` brings up.
The two skips are named for what they skip rather than bundled behind one "rehearsal" flag: a
laptop has no registry to pull `:local` from, and `docker image prune` is host-wide rather than
project-scoped, so on a laptop it would delete images belonging to other work.

```sh
make up
ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
    SKIP_PULL=1 SKIP_PRUNE=1 ./deploy/deploy.sh local
```

| | |
|---|---|
| Replicas recreated | `app_blue`, `app_green`, one at a time, both healthy |
| `web` container id | unchanged, which is the backend-only release being gapless at the edge |
| `worker` container id | unchanged |
| `nginx -s reload` | absent, by design |
| Readiness through nginx | 200 |

**The first run found three bugs, which is the entire argument for running it**, and the same
argument the restore drill made a week earlier.

- `set_env` wrote the release tag back with `sed -i "s|...|"`. GNU sed reads `-i` as "in place, no
  backup"; BSD sed reads the next argument as the backup suffix, consumes the script as one, and
  fails. So the function that pins the tag every release depends on worked on the box and could not
  run anywhere else. It writes through a temp file now.
- The readiness check at the end, the one step that proves the release is actually serving, was
  hardcoded to `http://localhost/`. Deriving the port from `HTTP_PORT` is not enough either:
  `compose.ci.yml` pins `8080:80` outright, so under the overlay that variable and the published
  port disagree. It now asks `compose port web 80`, which is the mapping compose actually applied.
- Every replica logged `[ERROR] Control server error: [Errno 13] Permission denied: '/app/.gunicorn'`
  on every boot. Gunicorn 26 opens a control socket under the working directory by default and the
  image does not own `/app`. Nothing here uses that interface, so it is off. An ERROR line that is
  not an error is what teaches you to skim past the ones that are.

### Re-seeding

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env exec app_blue \
  python manage.py seed_demo --seed 1
```

**There is no `--reset`, and there cannot be.** `AuditEvent` is append-only, because a Postgres
trigger refuses `UPDATE` and `DELETE`, and `AuditEvent.actor` is `PROTECT`, so a customer who has done
anything cannot be deleted and their audit rows cannot even have the actor nulled. Three guarantees
meeting, all working as designed. Re-seeding therefore means an empty database:

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env down -v
./deploy/deploy.sh <sha>
```

### The admin

`/admin/` is IP-allowlisted in `deploy/nginx/admin-allowlist.conf` and closed by default. It is the
weakest surface on the box: Django admin is session auth with a password, and the TOTP enforced on
`/api/v1/auth/` does not apply to it. A stolen superuser password is the entire control, on a form
that can read every account in the ledger. Widen it while demoing, then narrow it again:

```sh
docker compose -f deploy/compose.yml --env-file deploy/.env exec web nginx -s reload
```

## What is deliberately not here

No Kubernetes, Terraform, autoscaling or managed database: one box, and the compose file is the
whole topology. No Sentry, metrics or tracing: there are health and readiness probes and structured
logs with request ids (ADR-0028), and the honest next step is a log shipper, not an agent. No S3 for
media (ADR-0039); the trigger that would invert that is a second app *host*, and the change is one
entry in `STORAGES`; two replicas on one box share the volume, so it has not been triggered.

The two app replicas are not high availability and are not capacity (ADR-0043). They exist so a
release does not drop requests. Postgres, Redis and the box itself remain single points of failure,
and the honest fix for those is a second machine.
