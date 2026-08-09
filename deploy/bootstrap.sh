#!/usr/bin/env bash
#
# One-time host setup for a fresh Ubuntu 24.04 box. Run once, as a sudo-capable user.
#
#     curl -fsSL <raw-url>/deploy/bootstrap.sh | bash     # or clone and run it
#
# Deliberately does not deploy anything. It prepares a machine; `deploy.sh` puts software on it.
# Keeping those separate means a re-run of this is safe, and a deploy does not silently reconfigure
# the host underneath itself.

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/banking}"

echo "→ packages"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gnupg ufw unattended-upgrades gpg awscli

echo "→ docker engine"
if ! command -v docker >/dev/null; then
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER"
    echo "  (log out and back in for group membership to take effect)"
fi

# A t3.small is 2 GB and the steady-state footprint is ~800 MB, so this is not for running the app —
# it is for the moments that spike: a `pg_dump` alongside everything else, or an image pull
# decompressing while all six containers are up. Without it those get the OOM killer, which picks
# the largest process, which is Postgres.
echo "→ swap"
if [[ ! -f /swapfile ]]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    # Prefer reclaiming cache to swapping a live process; the swap is insurance, not tiering.
    sudo sysctl -w vm.swappiness=10 >/dev/null
    echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null
fi

echo "→ firewall"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# 5432 and 6379 are deliberately absent. Compose publishes neither; this is the second lock on the
# same door, for the day somebody adds a `ports:` line while debugging and forgets to remove it.
sudo ufw --force enable

echo "→ unattended security updates"
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

echo "→ certbot"
sudo snap install core >/dev/null 2>&1 || true
sudo snap refresh core >/dev/null 2>&1 || true
sudo snap install --classic certbot >/dev/null 2>&1 || true
sudo ln -sf /snap/bin/certbot /usr/bin/certbot
sudo mkdir -p /var/www/certbot

echo "→ ${APP_DIR}"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"

cat <<'NEXT'

Done. Next, in order:

  1. Put the repo in /srv/banking (git clone, or rsync the deploy/ directory).
  2. cp deploy/.env.example deploy/.env  and fill it in.
     - DJANGO_SECRET_KEY and FIELD_ENCRYPTION_KEYS have no defaults and the app will not boot
       without them. Back the keyring up somewhere that is not this machine: losing it means
       losing every account number and TOTP secret in the database.
     - DOMAIN and CSRF_TRUSTED_ORIGINS must match the certificate you are about to request.
  3. Point the domain's A record at this box and wait for it to resolve.
  4. sudo certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN"
  5. ./deploy/deploy.sh <sha>
  6. Add the renewal hook and the nightly backup:
       echo 'docker compose -f /srv/banking/deploy/compose.yml exec web nginx -s reload' \
         | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
       sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
       (crontab -l 2>/dev/null; echo '0 3 * * * /srv/banking/deploy/backup.sh >> /var/log/banking-backup.log 2>&1') | crontab -
  7. Run ./deploy/restore.sh against the first backup. Once. Today.

NEXT
