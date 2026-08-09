#!/usr/bin/env bash
#
# Ship a release. Run on the box, from /srv/banking.
#
#     ./deploy/deploy.sh <git-sha>
#
# Pull, restart, wait for readiness, prune. Not a rebuild — the images were built and tested by CI
# (ADR-0033), and a `t3.small` doing `npm ci` plus a Vite build would OOM on the Node step anyway.
# Building here would also mean deploying an artifact nothing had tested.
#
# Rollback is this same script with an older SHA. That is the whole reason compose pins `${IMAGE_TAG}`
# and never `latest`: with a floating tag, "roll back" and "rebuild" are the same command and neither
# is reproducible.

set -euo pipefail

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
    echo "usage: $0 <image-tag>   (a git sha; see 'docker images' for what is available)" >&2
    exit 2
fi

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f deploy/compose.yml --env-file deploy/.env)

echo "→ deploying ${TAG}"

# Written back so a subsequent bare `docker compose up` uses the same tag rather than silently
# reverting to whatever the file said before.
if grep -q '^IMAGE_TAG=' deploy/.env; then
    sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${TAG}|" deploy/.env
else
    echo "IMAGE_TAG=${TAG}" >> deploy/.env
fi

echo "→ pulling"
"${COMPOSE[@]}" pull --quiet

# `--remove-orphans` matters across releases that add or drop a service: without it, a container
# from the previous compose file keeps running, holding a port and answering requests from code
# nobody is looking at.
echo "→ starting"
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
        "${COMPOSE[@]}" logs --tail 80 app >&2
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
