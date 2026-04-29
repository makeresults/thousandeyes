# ThousandEyes Fake Data Generator for Splunk

> ⚠️ **Internal lab use only.** This generator is built for personal labs and
> Splunk dev/test environments with self-signed certificates. SSL verification
> is **deliberately disabled by default** — never run this against production
> Splunk without setting `--verify-ssl` and providing a trusted CA.

A self-contained, one-shot installer that emits realistic, ThousandEyes
API v7-shaped events to a Splunk HTTP Event Collector (HEC). Useful for
building dashboards, testing alerts, or showing off ThousandEyes-style
visualizations when you don't have a real ThousandEyes account.

---

## What's in the box

| File | Purpose |
|---|---|
| `thousandeyes-gen-installer.sh` | Bash installer that asks where Splunk lives, optionally creates the HEC index + token via the Splunk admin API, embeds the Python generator + Dockerfile, renders a `docker-compose.yml`, builds the image, and starts the container. |
| `generator/thousandeyes_generator.py` | The actual generator. Produces three event types (network, HTTP, alerts) and posts to HEC. Runs in Docker, but also works standalone. |
| `generator/Dockerfile.thousandeyes` | Minimal `python:3.12-slim` base — copies the generator and sets it as `ENTRYPOINT`. |

The installer **embeds** the generator and Dockerfile, so it can be moved to
any host as a single file and bootstrap the whole stack from there.

---

## Requirements

- `docker` with Compose v2 (Docker Desktop on macOS, Docker Engine 20.10+ on Linux)
- `bash` 4+
- `curl`
- A reachable Splunk instance with HEC enabled (port 8088 by default)

Tested on macOS (Docker Desktop) and Linux.

---

## Quick start

```bash
# Interactive install — prompts for Splunk host, HEC URL, token, and index
./thousandeyes-gen-installer.sh

# Other modes
./thousandeyes-gen-installer.sh --uninstall   # stop and remove the container
./thousandeyes-gen-installer.sh --status      # show container state
./thousandeyes-gen-installer.sh --logs        # follow container logs
./thousandeyes-gen-installer.sh --help        # full usage
```

By default the installer creates everything in `~/thousandeyes-gen/` and
starts a container named `thousandeyes-gen`. Override with:

```bash
INSTALL_DIR=/opt/te-gen ./thousandeyes-gen-installer.sh
```

### Splunk auto-setup (optional)

If you give the installer admin credentials, it will:

1. Create the target index (default: `thousandeyes`) if missing
2. Create a HEC token named `thousandeyes-gen` and use it automatically

Admin credentials are read interactively (`read -rs`) and **never written to
disk**.

---

## Generator CLI (running it directly)

The generator can also be run outside Docker — useful for `--dry-run`
testing or one-off bursts.

```bash
python3 generator/thousandeyes_generator.py [options]
```

| Flag | Default | Description |
|---|---|---|
| `--splunk-url URL` | `https://localhost:8088` | HEC base URL |
| `--token TOKEN` | (required, unless `--dry-run`) | HEC token |
| `--index NAME` | `thousandeyes` | Target Splunk index |
| `--interval SEC` | `10` | Seconds between batches in continuous mode |
| `--burst N` | `0` | Send N events and exit (0 = continuous) |
| `--dry-run` | off | Print events to stdout instead of POSTing |
| `--verify-ssl` | **off** | Enable SSL certificate validation (see security note) |
| `--skip-hec-wait` | off | Don't wait for `/health` before sending |
| `--hec-wait-max SEC` | `600` | How long to wait for HEC to come up |

### Examples

```bash
# Burst 200 events into a local lab Splunk
python3 generator/thousandeyes_generator.py \
    --splunk-url https://localhost:8088 \
    --token <hec_token> \
    --burst 200

# Continuous, every 5 seconds, against a remote dev Splunk with valid CA
python3 generator/thousandeyes_generator.py \
    --splunk-url https://splunk-dev.lab.local:8088 \
    --token <hec_token> \
    --interval 5 \
    --verify-ssl

# Dry run — see what events look like
python3 generator/thousandeyes_generator.py --dry-run --burst 5
```

---

## What it generates

The generator emits three event types that mirror the ThousandEyes API v7
schema, posted to HEC under different sourcetypes so you can split them
cleanly in SPL.

### 1. Agent-to-Server network tests
Latency, jitter, packet loss, path-trace summary. 8 fixed test definitions
(WAN, MPLS, SD-WAN, subsea cable, Internet, cloud) across 8 fixed agents
(Oslo, Stavanger, Bergen, London, Frankfurt, Houston, Singapore, Rio).

### 2. HTTP Server tests
Response time (with DNS/connect/SSL/wait/transfer breakdown), status code,
availability percent. 6 fixed test definitions covering common enterprise
SaaS (SAP, ServiceNow, O365, Teams, intranet, SCADA).

### 3. Alert notifications
Threshold breaches tied to 6 alert rules (latency, loss, HTTP 5xx, slow
response, jitter, path change). Triggered probabilistically based on
event degradation.

### Realism

Each test has a stable per-process **baseline** (e.g. ~30 ms latency on a
given test) with ±15% jitter on every event. About 5 % of events are
"minor degradation" (2–4× normal) and 3 % are "major" (5–15× normal,
loss, HTTP 5xx). This produces dashboards that aren't flat — they have
believable spikes and incidents.

---

## Security notes

This is **not** production-grade software. Specifically:

- **SSL verification is off by default** (`ctx.verify_mode = ssl.CERT_NONE`).
  This is intentional for self-signed lab Splunk instances. Use
  `--verify-ssl` against any environment with a real certificate chain.
- **HEC tokens are passed via `--token` (CLI arg)**, which means they are
  visible in `ps`, shell history, and Docker inspect. For a lab this is
  acceptable; for anything else, modify the generator to read the token
  from an env var or mounted secret file.
- **Splunk admin credentials** (used by the installer's optional
  auto-setup) are read interactively and never persisted, but a user
  with shell access during install could still snoop them. Run on a
  trusted host.

If you ever consider running this beyond a lab, at minimum:
1. Pass the HEC token via env var, not CLI arg
2. Pin a CA bundle and use `--verify-ssl`
3. Restrict the HEC token's allowed indexes and sourcetypes
4. Rotate the token periodically

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HEC rejected the token (HTTP 401/403)` | Wrong token or token disabled | Re-check token in Splunk → Settings → Data inputs → HTTP Event Collector |
| `connection refused` | HEC not enabled or wrong port | Verify HEC is enabled and listening on the URL you provided |
| Container exits immediately | Missing `--token` | Pass `--token` in the generated `docker-compose.yml` |
| Events not visible in search | Wrong index, or token has no access to index | `index=thousandeyes` (default) — verify the token's "Allowed indexes" |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Self-signed cert + `--verify-ssl` | Either drop `--verify-ssl` (lab) or supply a CA bundle |

---

## License

Personal/internal use. Not licensed for redistribution.
