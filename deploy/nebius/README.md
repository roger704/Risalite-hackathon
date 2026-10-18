# Public judges' deployment

The whole application runs on one dedicated Ubuntu 24.04 Nebius VM: `cpu-d3`, `2vcpu-8gb`, 40 GiB SSD, in the assigned hackathon project. It needs no GPU. Caddy terminates HTTPS and proxies the UI, API and streaming agent events to a single FastAPI worker listening only on `127.0.0.1:8000`.

Provisioning and public access require approval; these files do not create a VM automatically. Cloud resource charges and Nebius/Tavily research usage are separate; account credits and exact regional prices must be checked in the cloud console.

## Setup

1. Provision the VM with the reviewed `cloud-init.yaml.example` (replace its public-key and operator-IP placeholders). SSH uses a key-only deployment user with sudo; root login and password authentication are disabled. Allow public TCP 80/443 and restrict TCP 22 to the operator's IP. Do not expose port 8000.
2. Clone this repository into `/opt/spotwise` and run `sudo bash /opt/spotwise/deploy/nebius/bootstrap.sh`. The script prepares dependencies and service files without starting the app.
3. Install an owner-only `/etc/spotwise.env` (`root:root`, mode `0600`) with `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `NEBIUS_MODEL`, `NEBIUS_BASE_URL` and `RISALITE_RESEARCH_MODEL`, `RISALITE_SKEPTIC_MODEL`, `RISALITE_WRITER_MODEL`, `RISALITE_REVIEW_MODEL`. Use the same validated model for all roles initially. Never commit this file.
4. Copy the validated local `data/catalog.sqlite`, `data/normalized/` and `data/raw/` to `/var/lib/spotwise`, or import the required datasets there using the ingestion CLI. Do not copy local research runs, locks, credentials or `.env` files into the public checkout. Set ownership to `spotwise:spotwise`. With a running local importer, use SQLite's backup API rather than copying the catalog during writes.
5. Import at least `berlin_lor`, `berlin_lor_names`, `berlin_population`, `osm_pois`, and `berlin_centers`. For complete café metrics also import `vbb_gtfs` and `berlin_population_density_blocks`. Property and childcare flows need their own datasets; unavailable values must remain labeled as missing.
6. Choose a DNS name resolving to the VM's public IP. A temporary `spotwise.<IP-with-dashes>.sslip.io` name avoids buying a domain. Set `SPOTWISE_DOMAIN` in a systemd override for Caddy (`[Service]` then `Environment=SPOTWISE_DOMAIN=...`). Run `sudo systemctl daemon-reload` and `sudo -u caddy env SPOTWISE_DOMAIN=... caddy validate --config /etc/caddy/Caddyfile`.
7. Start with `sudo systemctl enable --now spotwise caddy`. Verify HTTPS `/health`, `/`, `/landing/`, `/research`, streaming events, results, maps, citations and exports. Run one live question through approval to completion before sharing the URL.

## Public research limits

Anyone with the link can plan and approve research. The service allows **one active run** and **20 admitted runs total** for this review deployment. The total is stored in `/var/lib/spotwise/runs/public-budget.sqlite` and survives restarts; an unavailable budget fails closed. Each admitted run can still spend up to the pipeline's 40-search and 40-extract limits plus model inference. This is a run-count cap, not a monetary cap.

Change `RISALITE_PUBLIC_MAX_RUNS` only deliberately in a systemd override. Set it to `0` to stop new live research while leaving the UI available. A repeated admission of the same run does not count twice. Keep one worker: live run state is process-local. Files persist across a restart, but an in-progress run cannot automatically resume and API run sessions must be recreated; wait for active runs to finish before updates.

## Operation

Use `sudo journalctl -u spotwise -u caddy` for logs. The provider keys stay in the server environment. Runtime snapshots, results and the budget are outside the Git checkout.

For updates: wait for active runs to finish, fetch the intended main commit, run the locked dependency sync, and restart `spotwise`. Keep the prior Git commit for rollback. To stop hosting charges, delete the VM and its boot disk in the Nebius console after judging; stopping only the app leaves VM charges running. Retain any results needed before deleting the disk.
