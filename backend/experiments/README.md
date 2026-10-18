# Deep research experiment

A minimal, portable research loop: a [DeepAgents](https://github.com/langchain-ai/deepagents) agent reasons with a model on **Nebius Token Factory** (`langchain-nebius`) and searches the live web with **Tavily** (`langchain-tavily`). It has no Risalite business logic yet.

```
question ─▶ Deep Agent ─▶ Nebius Token Factory (reasoning) ─▶ Tavily (web_search) ─▶ sourced markdown report
```

This directory is an isolated Python 3.12 project (`pyproject.toml` + `uv.lock`); it does not change the main backend's Python 3.11 environment or CI. The same `deep_research.py` runs locally, in Docker, and on a Nebius Cloud VM. Inference happens on Token Factory, so no GPU or local model is needed anywhere.

## Configuration

Copy `.env.example` to `.env` in this directory and fill in the keys. `.env` is ignored by Git and by the Docker build context; never commit it or bake it into an image.

| Variable | Default |
| --- | --- |
| `NEBIUS_API_KEY` | required |
| `NEBIUS_BASE_URL` | `https://api.tokenfactory.nebius.com/v1` |
| `NEBIUS_MODEL` | `MiniMaxAI/MiniMax-M3` |
| `TAVILY_API_KEY` | required |

## Run locally

```sh
cd backend/experiments
uv sync                     # creates .venv with Python 3.12
set -a; . ./.env; set +a    # load keys into this shell only
uv run python deep_research.py "Research specialty coffee competition in Neukölln, Berlin"
```

The report goes to stdout; progress and a verification line go to stderr, for example:

```
[tavily] search #1: specialty coffee competition Neukölln Berlin
...
[verify] nebius_llm_calls=5 tokens_in=76975 tokens_out=3010 tavily_searches=12 tavily_urls=48 cited_urls=20 cited_from_tavily=20 elapsed_s=20.1
[verify] OK
```

The script exits with status 2 unless Token Factory inference happened, Tavily was called, and the report cites at least one URL that Tavily actually returned. Options: `--max-searches` (Tavily budget, default 12), `--max-results` (results per search, default 5), `--recursion-limit` (agent steps, default 150).

## Run in Docker

```sh
cd backend/experiments
docker build -t risalite-research .
docker run --rm --env-file .env risalite-research \
  "Research specialty coffee competition in Neukölln, Berlin"
```

The image is `python:3.12-slim`, installs only the locked runtime dependencies, runs as a non-root user, and contains no credentials; they are supplied at run time with `--env-file`.

## Deploy on a CPU-only Nebius Cloud VM

Nebius Cloud only hosts this container; the model runs on Token Factory. No GPU, Kubernetes, or extra infrastructure is needed.

### 1. Create the VM

In the [Nebius console](https://console.nebius.com): **Compute → Virtual machines → Create VM**, then choose a non-GPU platform (`cpu-d3`; preset `2vcpu-8gb` is enough), the **Ubuntu 24.04 LTS** (`ubuntu24.04-driverless`) boot image, a public IP address, and your SSH public key with a username other than `root` or `admin`.

Or with the [Nebius CLI](https://docs.nebius.com/cli) (see [Create a VM](https://docs.nebius.com/compute/virtual-machines/manage)):

```sh
export USER_DATA=$(jq -Rrs '.' <<EOF
#cloud-config
users:
  - name: $USER
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat ~/.ssh/id_ed25519.pub)
EOF
)
nebius vpc subnet list   # note your subnet ID
nebius compute instance create \
  --name risalite-research \
  --resources-platform cpu-d3 \
  --resources-preset 2vcpu-8gb \
  --boot-disk-managed-disk-name risalite-research-boot \
  --boot-disk-managed-disk-type network_ssd \
  --boot-disk-managed-disk-size-gibibytes 30 \
  --boot-disk-managed-disk-source-image-family-image-family ubuntu24.04-driverless \
  --boot-disk-attach-mode READ_WRITE \
  --cloud-init-user-data "$USER_DATA" \
  --network-interfaces '[{"name": "eth0", "ip_address": {}, "public_ip_address": {}, "subnet_id": "<subnet_ID>"}]'
```

### 2. SSH into the VM

```sh
PUBLIC_IP=$(nebius compute instance get-by-name --name risalite-research --format json \
  | jq -r '.status.network_interfaces[0].public_ip_address.address | split("/")[0]')
ssh "$USER@$PUBLIC_IP"
```

### 3. Clone, build, configure, and run

On the VM, [`scripts/deploy_nebius_vm.sh`](../../scripts/deploy_nebius_vm.sh) does every step: installs Docker, clones (or updates) the repository, builds the image, prompts for the two API keys without echoing them and writes them to an owner-only `.env`, then runs a research question:

```sh
curl -fsSLO https://raw.githubusercontent.com/roger704/Risalite-hackathon/main/scripts/deploy_nebius_vm.sh
bash deploy_nebius_vm.sh "Research specialty coffee competition in Neukölln, Berlin"
```

The equivalent manual steps:

```sh
sudo apt-get update && sudo apt-get install -y docker.io git
sudo usermod -aG docker "$USER" && newgrp docker
git clone https://github.com/roger704/Risalite-hackathon.git
cd Risalite-hackathon/backend/experiments
docker build -t risalite-research .
(umask 077 && cp .env.example .env) && nano .env   # paste the keys; never commit this file
docker run --rm --env-file .env risalite-research \
  "Research specialty coffee competition in Neukölln, Berlin"
```

### 4. Verify

A successful run prints a markdown report with inline source links and ends with `[verify] OK` on stderr: `nebius_llm_calls` > 0 confirms Token Factory inference, `tavily_searches` > 0 confirms Tavily was called, and `cited_from_tavily` > 0 confirms the report cites web evidence that Tavily returned. The exit status is 0 only in that case. Keys are read from the environment and are never printed.

Delete the VM when finished to stop charges (`nebius compute instance delete --id <VM_ID>`, or from the console).
