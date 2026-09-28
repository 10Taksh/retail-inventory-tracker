#!/usr/bin/env bash
# One-shot server setup for Ubuntu 22.04/24.04 on Oracle Cloud (or any VM).
#
#   sudo bash deploy/setup.sh
#
# Safe to re-run: it updates the checkout, reinstalls dependencies, and restarts the service.
# Optional:
#   DOMAIN=inventory.example.com        serve HTTPS for that hostname (remembered for later runs)
#   AUTH_USER=admin AUTH_PASS=secret    set (or change) the site login; one is generated otherwise

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/10Taksh/retail-inventory-tracker.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/retail-inventory
DATA_DIR=/var/lib/retail-inventory
ENV_FILE=/etc/retail-inventory.env
AUTH_FILE=/etc/retail-inventory.auth
DOMAIN="${DOMAIN:-}"
AUTH_USER="${AUTH_USER:-}"
AUTH_PASS="${AUTH_PASS:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git sqlite3 debian-keyring debian-archive-keyring apt-transport-https curl

if ! command -v caddy >/dev/null; then
  echo "==> Installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

echo "==> Opening ports 80/443 in the VM firewall (Oracle's Ubuntu image blocks them by default)"
# The ACCEPT rules must sit above Oracle's catch-all REJECT, whose line number varies by
# image, so find it rather than assuming a position.
for port in 80 443; do
  while iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; do
    iptables -D INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  done
  reject_line=$(iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')
  if [[ -n "$reject_line" ]]; then
    iptables -I INPUT "$reject_line" -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  else
    iptables -A INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
done
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save >/dev/null; fi

echo "==> Ensuring swap exists (small VMs have none; it prevents out-of-memory during installs)"
if [[ "$(swapon --show --noheadings | wc -l)" -eq 0 ]] && [[ ! -f /swapfile ]]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Creating service user and directories"
id -u inventory >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin inventory
mkdir -p "$APP_DIR" "$DATA_DIR/invoices" "$DATA_DIR/backups"

echo "==> Fetching the app ($REPO_URL @ $BRANCH)"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Installing Python dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Writing $ENV_FILE"
  cp "$APP_DIR/deploy/retail-inventory.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

chown -R inventory:inventory "$APP_DIR" "$DATA_DIR"

echo "==> Installing the systemd service"
cp "$APP_DIR/deploy/retail-inventory.service" /etc/systemd/system/retail-inventory.service
systemctl daemon-reload
systemctl enable --now retail-inventory
systemctl restart retail-inventory

echo "==> Configuring the site login"
NEW_PASSWORD=""
if [[ -n "$AUTH_PASS" || ! -f "$AUTH_FILE" ]]; then
  AUTH_USER="${AUTH_USER:-admin}"
  if [[ -z "$AUTH_PASS" ]]; then
    AUTH_PASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
    NEW_PASSWORD="$AUTH_PASS"
  fi
  printf '%s\n%s\n' "$AUTH_USER" "$(caddy hash-password --plaintext "$AUTH_PASS")" > "$AUTH_FILE"
  chmod 600 "$AUTH_FILE"
fi
AUTH_USER=$(sed -n 1p "$AUTH_FILE")
AUTH_HASH=$(sed -n 2p "$AUTH_FILE")

echo "==> Configuring Caddy"
# Remember the domain so plain re-runs (to deploy updates) keep serving HTTPS.
DOMAIN_FILE=/etc/retail-inventory.domain
if [[ -n "$DOMAIN" ]]; then echo "$DOMAIN" > "$DOMAIN_FILE"; elif [[ -f "$DOMAIN_FILE" ]]; then DOMAIN=$(cat "$DOMAIN_FILE"); fi
SITE="${DOMAIN:-:80}"
sed -e "s|^:80 {|$SITE {|" \
    -e "s|__AUTH_USER__|$AUTH_USER|" \
    -e "s|__AUTH_HASH__|$AUTH_HASH|" \
  "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

echo "==> Installing nightly backup (03:15, keeps 14 days)"
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/retail-inventory-backup
cat > /etc/cron.d/retail-inventory-backup <<'CRON'
15 3 * * * inventory /usr/local/bin/retail-inventory-backup
CRON

echo
echo "Done. Health check:"
sleep 2
curl -fsS http://127.0.0.1:8000/api/health && echo
PUBLIC_IP=$(curl -fsS --max-time 3 -H 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/vnics/ 2>/dev/null | python3 -c 'import json,sys; v=json.load(sys.stdin); print(next((x.get("publicIp") for x in v if x.get("publicIp")), ""))' 2>/dev/null || true)
PUBLIC_IP=${PUBLIC_IP:-$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)}
if [[ -n "$DOMAIN" ]]; then echo "Open: https://$DOMAIN/"; else echo "Open: http://${PUBLIC_IP:-<your-public-ip>}/"; fi
if [[ -n "$NEW_PASSWORD" ]]; then
  echo "Login: $AUTH_USER / $NEW_PASSWORD   (shown once - save it now)"
else
  echo "Login: $AUTH_USER (password unchanged; set a new one with: sudo AUTH_PASS=... bash $APP_DIR/deploy/setup.sh)"
fi
if ! grep -qE '^GEMINI_API_KEY=.+' "$ENV_FILE"; then
  echo
  echo "!! GEMINI_API_KEY is not set - uploads will fail until you add it:"
  echo "   sudo nano $ENV_FILE    then    sudo systemctl restart retail-inventory"
fi
echo "Logs: journalctl -u retail-inventory -f"
