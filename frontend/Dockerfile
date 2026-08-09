# syntax=docker/dockerfile:1.7
#
# The web image: the built SPA plus the nginx that serves it and proxies everything else.
#
# The bundle is baked in rather than shared through a volume (ADR-0035). A volume would mean the
# bundle and the nginx config that serves it are versioned separately, and the failure mode is
# silent — a half-updated volume serving last release's `index.html`, which points at chunk names
# the new release does not have. One image, one atomic thing to roll back.
#
# Build from the repo root:
#     docker build -f frontend/Dockerfile -t banking-web .

# ------------------------------------------------------------------------------------------------
# Stage 1 — build the bundle
# ------------------------------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app

# Lockfile first, for the same reason the backend image installs dependencies before source.
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` and not `npm install`: it installs exactly the lockfile and fails if package.json has
# drifted from it, which is the property that makes a CI build reproducible.
RUN npm ci

COPY frontend/ ./
# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the image build rather than
# shipping a bundle CI would have caught.
RUN npm run build

# ------------------------------------------------------------------------------------------------
# Stage 2 — nginx
# ------------------------------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# The base image's entrypoint runs `envsubst` over anything in templates/ and writes the result to
# /etc/nginx/conf.d/. That is how ${DOMAIN} reaches the config without a config-generation script
# of our own.
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/app.conf.template /etc/nginx/templates/app.conf.template
COPY deploy/nginx/admin-allowlist.conf /etc/nginx/admin-allowlist.conf
# A plain include rather than a template: it uses nginx's own $host, which needs no substitution.
COPY deploy/nginx/security-headers.conf /etc/nginx/security-headers.conf

COPY --from=builder /app/dist /usr/share/nginx/html

# Only the variables named here are substituted. Left unlisted, `envsubst` would also eat nginx's
# own `$host`, `$uri` and `$proxy_add_x_forwarded_for` — replacing every one with an empty string
# and producing a config that starts cleanly and proxies nothing correctly.
ENV NGINX_ENVSUBST_FILTER="DOMAIN"
ENV DOMAIN=localhost

EXPOSE 80 443

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "--quiet", "--tries=1", "--spider", "http://127.0.0.1/healthz"]
