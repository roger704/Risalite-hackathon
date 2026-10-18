#!/usr/bin/env bash
# Run ON a CPU-only Ubuntu 24.04 Nebius Cloud VM: installs Docker, clones or updates the
# repository, builds the research image, creates a private .env if missing, and runs a
# smoke-test research question. Usage:
#   bash deploy_nebius_vm.sh ["research question"]
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/roger704/Risalite-hackathon.git}"
REPO_DIR="${REPO_DIR:-$HOME/Risalite-hackathon}"
REPO_REF="${REPO_REF:-main}"
IMAGE="${IMAGE:-risalite-research}"
QUESTION="${1:-Research specialty coffee competition in Neukölln, Berlin}"

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io git
  sudo systemctl enable --now docker
fi
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
fi

if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch --quiet origin "$REPO_REF"
  git -C "$REPO_DIR" checkout --quiet "$REPO_REF"
  git -C "$REPO_DIR" pull --quiet --ff-only origin "$REPO_REF"
else
  git clone --quiet --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR/backend/experiments"

"${DOCKER[@]}" build -t "$IMAGE" .

if [ ! -f .env ]; then
  read -rsp "NEBIUS_API_KEY: " nebius_key && echo
  read -rsp "TAVILY_API_KEY: " tavily_key && echo
  (
    umask 077
    {
      echo "NEBIUS_API_KEY=$nebius_key"
      echo "NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1"
      echo "NEBIUS_MODEL=MiniMaxAI/MiniMax-M3"
      echo "TAVILY_API_KEY=$tavily_key"
    } > .env
  )
  unset nebius_key tavily_key
fi

"${DOCKER[@]}" run --rm --env-file .env "$IMAGE" "$QUESTION"
