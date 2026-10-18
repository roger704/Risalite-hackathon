#!/usr/bin/env bash
# Run as root on the dedicated Ubuntu VM. Does not start services or install keys.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo "Run this setup as root on the dedicated demo VM." >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git curl ca-certificates python3-venv caddy ufw
id spotwise >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/spotwise --shell /usr/sbin/nologin spotwise
install -d -o spotwise -g spotwise -m 0750 /var/lib/spotwise
if [ ! -d /opt/spotwise/.git ]; then
  git clone https://github.com/roger704/Risalite-hackathon.git /opt/spotwise
fi
python3 -m venv /opt/spotwise/bootstrap
/opt/spotwise/bootstrap/bin/pip install 'uv==0.10.4'
cd /opt/spotwise/backend
UV_CACHE_DIR=/opt/spotwise/uv-cache UV_PYTHON_INSTALL_DIR=/opt/spotwise/python /opt/spotwise/bootstrap/bin/uv sync --locked --no-dev --python 3.11
# The unprivileged service can read code, but cannot modify the checkout or secrets.
chmod -R a+rX /opt/spotwise/backend/.venv /opt/spotwise/python
install -m 0644 /opt/spotwise/deploy/nebius/spotwise.service /etc/systemd/system/spotwise.service
install -m 0644 /opt/spotwise/deploy/nebius/Caddyfile /etc/caddy/Caddyfile
systemctl daemon-reload
echo "Prepared. Install /etc/spotwise.env, set SPOTWISE_DOMAIN for Caddy, and import data before starting SpotWise."
