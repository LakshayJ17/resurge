#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the Resurge web app (docs/DEPLOY.md). Safe to run again.
#
#   curl -fsSL https://raw.githubusercontent.com/LakshayJ17/resurge/main/deploy/setup-server.sh | bash
#
# Adds swap (free 1 GB servers need it), installs Docker and git, and clones or updates the code in ~/resurge.
# It never creates, reads or changes .env: put your API keys and hosted settings there yourself.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/LakshayJ17/resurge.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-$HOME/resurge}"
SWAP_GB="${SWAP_GB:-4}"

echo "==> Swap"
if [ -z "$(swapon --show --noheadings)" ]; then
  sudo fallocate -l "${SWAP_GB}G" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "    added ${SWAP_GB} GB of swap"
else
  echo "    already on: $(swapon --show --noheadings | awk '{print $1, $3}' | paste -sd ' ')"
fi

echo "==> Docker and git"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1 || ! docker buildx version >/dev/null 2>&1; then
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-v2 docker-buildx git
fi
sudo systemctl enable --now docker >/dev/null
if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  echo "    added $USER to the docker group (takes effect at the next log-in)"
fi
echo "    $(sudo docker --version), $(sudo docker compose version)"

echo "==> Code in $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

if [ -f "$APP_DIR/.env" ]; then
  echo "==> Ready. Start it with: cd $APP_DIR && docker compose --profile caddy pull && docker compose --profile caddy up -d"
else
  echo "==> Next: create $APP_DIR/.env (see docs/DEPLOY.md), then start it with:"
  echo "    cd $APP_DIR && docker compose --profile caddy pull && docker compose --profile caddy up -d"
fi
