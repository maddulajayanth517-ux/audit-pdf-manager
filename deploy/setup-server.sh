#!/usr/bin/env bash
# One-command setup for an Ubuntu server (intended for Oracle Cloud Always Free, Ubuntu 22.04/24.04).
#   curl -fsSL https://raw.githubusercontent.com/maddulajayanth517-ux/audit-pdf-manager/main/deploy/setup-server.sh | bash
# Safe to run again: it updates the code and restarts the app.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/maddulajayanth517-ux/audit-pdf-manager.git}"
APP_DIR="${APP_DIR:-$HOME/audit-pdf-manager}"

echo "==> 1/6 Installing Docker and git (if missing)"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo apt-get update -qq && sudo apt-get install -y -qq git iptables-persistent >/dev/null 2>&1 || \
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git iptables-persistent
sudo systemctl enable --now docker

echo "==> 2/6 Getting the code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR/deploy"

echo "==> 3/6 Opening ports 80 and 443 in the server firewall"
# Oracle's Ubuntu images block everything except SSH in iptables; the cloud Security List must also allow 80/443.
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save >/dev/null 2>&1 || true

echo "==> 4/6 Configuration"
if [ ! -f .env ]; then
  PUBLIC_IP="$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)"
  DEFAULT_DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  read -r -p "Web address [${DEFAULT_DOMAIN}]: " DOMAIN </dev/tty || true
  DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
  while true; do
    read -r -s -p "Choose the login password (min. 10 characters): " PW </dev/tty; echo
    [ "${#PW}" -ge 10 ] && break
    echo "Too short, try again."
  done
  umask 077
  printf 'DOMAIN=%s\nAPP_USERNAME=audit\nAPP_PASSWORD=%s\n' "$DOMAIN" "$PW" > .env
  echo "Saved to $APP_DIR/deploy/.env (readable only by you)."
fi
DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2-)"

echo "==> 5/6 Building and starting (first build takes a few minutes)"
sudo docker compose --env-file .env -f docker-compose.server.yml up -d --build

echo "==> 6/6 Waiting for the app"
for _ in $(seq 1 60); do
  if sudo docker compose -f docker-compose.server.yml exec -T app \
       python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" >/dev/null 2>&1; then
    echo
    echo "Done. Open:  https://${DOMAIN}"
    echo "Login: username 'audit' + the password you chose. (The HTTPS certificate can take ~1 minute.)"
    exit 0
  fi
  sleep 5; printf '.'
done
echo; echo "The app did not report healthy yet. Check: sudo docker compose -f $APP_DIR/deploy/docker-compose.server.yml logs --tail 50"
exit 1
