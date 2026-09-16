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

set -euo pipefail

TAG="${1:-}"
WEB_TAG_ARG="${2:-}"
if [[ -z "$TAG" ]]; then
    echo "usage: $0 <image-tag> [web-tag]   (git shas; see 'docker images' for what is available)" >&2
    exit 2
fi

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f deploy/compose.yml --env-file deploy/.env)

echo "→ deploying ${TAG}"

# Written back so a subsequent bare `docker compose up` uses the same tag rather than silently
# reverting to whatever the file said before.
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" deploy/.env; then
        sed -i "s|^${key}=.*|${key}=${value}|" deploy/.env
    else
        echo "${key}=${value}" >> deploy/.env
    fi
}

set_env IMAGE_TAG "$TAG"
# Only when given. Leaving WEB_TAG alone is what makes a backend-only release leave the edge
# container untouched, and therefore gapless.
if [[ -n "$WEB_TAG_ARG" ]]; then
    set_env WEB_TAG "$WEB_TAG_ARG"
fi

echo "→ pulling"
"${COMPOSE[@]}" pull --quiet

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
echo -n "→ waiting for readiness "
for attempt in $(seq 1 60); do
    if curl -fsS -o /dev/null http://localhost/api/v1/ready/; then
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
echo "→ pruning old images"
docker image prune -f >/dev/null

"${COMPOSE[@]}" ps
echo "✓ ${TAG} is live"
