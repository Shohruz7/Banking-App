#!/usr/bin/env bash
#
# Ship a release. Run on the box, from /srv/banking.
#
#     ./deploy/deploy.sh <git-sha> [web-tag]
#
# Pull, migrate, roll the app replicas one at a time, converge the rest, wait for readiness, prune.
# Not a rebuild — the images were built and tested by CI (ADR-0033), and a `t3.small` doing `npm ci`
# plus a Vite build would OOM on the Node step anyway. Building here would also mean deploying an
# artifact nothing had tested.
#
# **The API and the WebSocket roll without dropping a request (ADR-0043).** The edge does not: `web`
# owns port 80, and while it is being recreated the port is unbound. That is why it has its own tag
# — a backend-only release leaves it alone, and a frontend release still blips for about a second.
# Pass the web tag as the second argument, or omit it to leave `web` on whatever it is running.
#
# Rollback is this same script with an older SHA. That is the whole reason compose pins `${IMAGE_TAG}`
# and never `latest`: with a floating tag, "roll back" and "rebuild" are the same command and neither
# is reproducible.
#
# ── Rehearsing it ──────────────────────────────────────────────────────────────────────────────
#
# **A deploy script nobody has run is a release plan, not a deploy script.** `restore.sh` sat in that
# state until it was rehearsed, and the first run found two bugs in ten minutes. So this takes the
# same overrides that file does, and the drill is part of the runbook rather than a thing to work out
# during an outage:
#
#     make up
#     ENV_FILE=deploy/.env.ci COMPOSE_OVERRIDE=deploy/compose.ci.yml \
#       SKIP_PULL=1 SKIP_PRUNE=1 ./deploy/deploy.sh local
#
# `SKIP_PULL` and `SKIP_PRUNE` are named for what they skip rather than bundled behind one
# "rehearsal" flag, because a rehearsal that silently differs from production in ways you cannot
# enumerate is worth much less than one whose differences are in the command line.

set -euo pipefail

TAG="${1:-}"
WEB_TAG_ARG="${2:-}"
if [[ -z "$TAG" ]]; then
    echo "usage: $0 <image-tag> [web-tag]   (git shas; see 'docker images' for what is available)" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

# Which stack to act on. The defaults are the production compose file and its `.env`, which is what
# the runbook and the deploy workflow use; the overrides exist for the drill above. Same shape as
# backup.sh and restore.sh, deliberately — three scripts that act on the same stack should not each
# have their own idea of how to be pointed at it.
ENV_FILE="${ENV_FILE:-deploy/.env}"
COMPOSE=(docker compose -f deploy/compose.yml)
# An `if` rather than `[[ ... ]] && ...`: under `set -e` the short-circuit form evaluates to 1 when
# the variable is unset, which is harmless here only because another statement follows it.
if [[ -n "${COMPOSE_OVERRIDE:-}" ]]; then
    COMPOSE+=(-f "$COMPOSE_OVERRIDE")
fi
COMPOSE+=(--env-file "$ENV_FILE")

echo "→ deploying ${TAG}"

# Written back so a subsequent bare `docker compose up` uses the same tag rather than silently
# reverting to whatever the file said before.
#
# **Not `sed -i`, and the difference is not cosmetic.** GNU sed reads `-i` as "edit in place with no
# backup"; BSD sed reads the next argument as the backup suffix, so `sed -i "s|...|"` consumes the
# script *as* the suffix and then fails with no expression. The GNU form therefore works on the box
# and breaks on a laptop, which is the same GNU/BSD divergence that stopped `date -uIs` in backup.sh
# and exactly the class of bug a rehearsal exists to find. A temp file works identically on both.
set_env() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"

    # Passed through the environment rather than `awk -v`, which processes backslash escapes in the
    # value before awk ever sees it. Tags do not contain backslashes today; this costs nothing and
    # means it never matters.
    if grep -q "^${key}=" "$ENV_FILE"; then
        _k="$key" _v="$value" awk '
            BEGIN { k = ENVIRON["_k"]; v = ENVIRON["_v"] }
            index($0, k "=") == 1 { print k "=" v; next }
            { print }
        ' "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi

    # `cat >` rather than `mv`: this file holds the key that decrypts account numbers, and `mv` would
    # replace it with mktemp's inode and mktemp's mode. Writing through the existing file keeps
    # whatever permissions it was given.
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

set_env IMAGE_TAG "$TAG"
# Only when given. Leaving WEB_TAG alone is what makes a backend-only release leave the edge
# container untouched, and therefore gapless.
if [[ -n "$WEB_TAG_ARG" ]]; then
    set_env WEB_TAG "$WEB_TAG_ARG"
fi

# Skipped only for the drill: a laptop runs against images `make images` built locally, and no
# registry has `banking-backend:local` to pull.
if [[ "${SKIP_PULL:-0}" == "1" ]]; then
    echo "→ skipping pull (SKIP_PULL=1)"
else
    echo "→ pulling"
    "${COMPOSE[@]}" pull --quiet
fi

# Schema first, on its own, and blocking. `run --rm` rather than `up -d migrate` because it
# propagates the exit code, so `set -e` stops the release here instead of rolling a replica onto a
# database that did not migrate.
#
# Note what this implies, because it is the constraint the whole rolling scheme buys with: between
# this line and the last one, the *previous* release's code is serving against the *new* schema.
# Every migration therefore has to be expand-only — add, never narrow or drop, and do the dropping
# in a later release once nothing runs the old code. CI refuses a migration that breaks this
# without an explicit marker.
echo "→ migrating"
"${COMPOSE[@]}" run --rm migrate

# One replica at a time. `--no-deps` so compose does not re-evaluate the anchor's
# `migrate: service_completed_successfully` and run it a second time; `--force-recreate` so
# redeploying the same tag is a real restart rather than a no-op; `--wait` so the next line does not
# start until *this* container's own HEALTHCHECK passes.
#
# There is no `nginx -s reload` in this script, and that is the design rather than an omission. The
# upstream is two literal addresses pinned by `ipv4_address`, so nginx has nothing to re-resolve. A
# reload here would be the most dangerous line in the file: with a peer down it fails to parse,
# never sends SIGHUP, and leaves nginx silently serving stale addresses.
for replica in app_blue app_green; do
    echo "→ rolling ${replica}"
    "${COMPOSE[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 150 "$replica"
    # Not a formality. The surviving replica is carrying all the traffic plus every WebSocket that
    # just reconnected off the one being replaced; rolling straight into the second swap would drop
    # those same sockets again before the client's backoff has reset.
    if [[ "$replica" == "app_blue" ]]; then
        sleep 15
    fi
done

# Everything that is not rolled. Celery is safe to cut hard — CELERY_TASK_ACKS_LATE means an
# interrupted task is redelivered rather than lost. `--remove-orphans` matters across releases that
# add or drop a service: without it a container from the previous compose file keeps running,
# answering requests from code nobody is looking at. On the release that introduces this scheme it
# is also what removes the old single `app` container — so that release is itself the last one with
# a gap.
echo "→ converging worker, beat, web"
"${COMPOSE[@]}" up -d --remove-orphans

# Readiness, not liveness: this returns 200 only once Postgres and the cache are actually reachable,
# so it covers the case where the app booted fine and the database did not.
#
# **The port is asked for, not assumed.** This probe was hardcoded to `http://localhost/`, which is
# right on the box and wrong everywhere else, so the one step that proves the release is actually
# serving was also the one step no rehearsal could exercise. Deriving it from `HTTP_PORT` is not
# good enough either, and that is the interesting part: `deploy/compose.ci.yml` pins `8080:80`
# outright, so under the overlay the published port and that variable disagree. `compose port` reads
# the mapping compose actually applied, whatever produced it.
if [[ -z "${READY_URL:-}" ]]; then
    published="$("${COMPOSE[@]}" port web 80 | head -1)"
    if [[ -z "$published" ]]; then
        echo "could not determine the port 'web' publishes; set READY_URL to probe it directly" >&2
        exit 1
    fi
    READY_URL="http://localhost:${published##*:}/api/v1/ready/"
fi

echo -n "→ waiting for readiness at ${READY_URL} "
for attempt in $(seq 1 60); do
    if curl -fsS -o /dev/null "$READY_URL"; then
        echo " ok"
        break
    fi
    if [[ $attempt -eq 60 ]]; then
        echo " FAILED"
        echo "--- app logs ---" >&2
        "${COMPOSE[@]}" logs --tail 80 app_blue app_green >&2
        echo "Release did not come up. The previous images are still on disk:" >&2
        echo "  ./deploy/deploy.sh <previous-sha>" >&2
        exit 1
    fi
    echo -n "."
    sleep 2
done

# Only after a healthy release. Pruning before the check would delete the image a rollback needs.
#
# Skipped for the drill because this is host-wide, not project-scoped: on the box every dangling
# image belongs to this stack, on a laptop most of them do not.
if [[ "${SKIP_PRUNE:-0}" == "1" ]]; then
    echo "→ skipping image prune (SKIP_PRUNE=1)"
else
    echo "→ pruning old images"
    docker image prune -f >/dev/null
fi

"${COMPOSE[@]}" ps
echo "✓ ${TAG} is live"
