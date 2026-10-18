# Public judges' deployment

Public homepage: **https://spotwise.81-85-0-244.sslip.io/**. The animated landing page introduces the project; **Try the demo** opens the research app at `/app/`. The landing page is also available at `/landing/`. Use the café location workflow for judging; the other examples need additional workflow-specific datasets and integration. Deployment prepared on 18 October 2026.

| Resource | Value |
|---|---|
| Project | `project-e05r0j44ln00v6vvzb69wj` (`group66-project`) |
| Region | `uk-south2` |
| VM | `computeinstance-e05xmkdtk0ach62585` (`spotwise-judges`) |
| Managed boot disk | `computedisk-e05dnv53m52629phjr` |
| Static public IP | `81.85.0.244` |
| Public IP allocation | `vpcallocation-e05sqt0cgdkwfx705n` |
| Operator SSH | `deploy@81.85.0.244`, restricted to `83.135.183.58` |
| App checkout | `/opt/spotwise` |
| Persistent data | `/var/lib/spotwise` |
| Research, skeptic, review and writer model | `nvidia/nemotron-3-super-120b-a12b` |

The whole application runs on one dedicated Ubuntu 24.04 Nebius VM: `cpu-d3`, `2vcpu-8gb`, 40 GiB SSD, in the assigned hackathon project. It needs no GPU. Caddy terminates HTTPS and proxies the UI, API and streaming agent events to a single FastAPI worker listening only on `127.0.0.1:8000`.

Caddy serves the landing page for the exact `/` path, while FastAPI serves the research app and its assets at `/app/`. API routes stay at their existing paths. The `/runs/*/events` route bypasses response compression and flushes immediately so research progress reaches the browser while a run is active. Ordinary pages and API responses remain compressed.

Live downloads use `/api/v1/runs/{id}/export/{format}`. Caddy also forwards the older `/runs/{id}/export/{format}` URLs for browser sessions opened before this correction, preserving their authorization headers and selected layer mix.

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

Anyone with the link can plan and approve research. The service allows **three active runs** and **60 admitted runs total** for this review deployment. The total is stored in `/var/lib/spotwise/runs/public-budget.sqlite` and survives restarts; an unavailable budget fails closed. Each admitted run can still spend up to the pipeline's 40-search and 40-extract limits plus model inference. This is a run-count cap, not a monetary cap.

Change `RISALITE_PUBLIC_MAX_RUNS` only deliberately in a systemd override. Set it to `0` to stop new live research while leaving the UI available. A repeated admission of the same run does not count twice. Keep one worker: live run state is process-local. Files persist across a restart, but an in-progress run cannot automatically resume and API run sessions must be recreated; wait for active runs to finish before updates.

## Validation — 18 October 2026

The public HTTPS app completed a live Nemotron café investigation for Kreuzberg with the market sub-question selected. All six scoring layers contained data for all 19 neighbourhood areas. Research, skeptic, review and writer calls used `nvidia/nemotron-3-super-120b-a12b`. Browser checks covered streaming progress, the ranked map, citation-to-source navigation, Markdown report download, and sources CSV download.

A controlled local 503 test confirmed that the question is retained and **Try again** recovers to a plan. Regression tests cover Unicode line separators in extracted evidence, grouped citation rendering and HTML escaping, plus persistent public-run admission limits.

## Operation

Use `sudo journalctl -u spotwise -u caddy` for logs. The provider keys stay in the server environment. Runtime snapshots, results and the budget are outside the Git checkout.

For updates: wait for active runs to finish, fetch the intended main commit, run the locked dependency sync, and restart `spotwise`. Keep the prior Git commit for rollback. To stop hosting charges, delete the VM and its boot disk in the Nebius console after judging; stopping only the app leaves VM charges running. Release the static public IP allocation too. Retain any results needed before deleting the disk. If the operator's public IP changes, update the SSH firewall rule through the cloud console before trying SSH again.
