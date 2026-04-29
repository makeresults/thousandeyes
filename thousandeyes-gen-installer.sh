#!/usr/bin/env bash
###############################################################################
# thousandeyes-gen-installer.sh
#
# Self-contained installer for the ThousandEyes fake-data generator.
# Embeds the Python generator + Dockerfile and renders a docker-compose.yml
# tailored to the user's Splunk-in-Docker setup.
#
# What it does
#   1. Asks where Splunk is running (host, container name on a Docker network,
#      or remote IP) and how to reach the HEC endpoint.
#   2. Optionally creates the target index and HEC token via Splunk's admin
#      API (so you don't have to click around in Splunk Web).
#   3. Writes Dockerfile + generator.py + docker-compose.yml to an install
#      directory (default: ~/thousandeyes-gen).
#   4. Builds the image and starts the container.
#
# Usage
#   ./thousandeyes-gen-installer.sh                # interactive install
#   ./thousandeyes-gen-installer.sh --install      # same as above
#   ./thousandeyes-gen-installer.sh --uninstall    # stop + remove container
#   ./thousandeyes-gen-installer.sh --status       # show container status
#   ./thousandeyes-gen-installer.sh --logs         # follow container logs
#   ./thousandeyes-gen-installer.sh --help         # show this help
#
# Requires: docker (with compose v2), bash 4+, curl
# Tested on: macOS (Docker Desktop), Linux (Docker Engine 20.10+)
###############################################################################
set -euo pipefail

# ─── Configuration (overridable via env) ─────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-$HOME/thousandeyes-gen}"
CONTAINER_NAME="thousandeyes-gen"

# ─── Colors ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    RED=$'\033[0;31m'
    BLUE=$'\033[0;34m'
    GREY=$'\033[0;90m'
    NC=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" BLUE="" GREY="" NC=""
fi

# ─── UI helpers ──────────────────────────────────────────────────────────────
banner() {
    local title="$1"
    local line="══════════════════════════════════════════════════════════════"
    echo "${BLUE}╔${line}╗${NC}"
    printf "${BLUE}║${NC}  %-60s${BLUE}║${NC}\n" "$title"
    echo "${BLUE}╚${line}╝${NC}"
}

step() { echo "${BLUE}▶${NC} $1"; }
info() { echo "${GREY}  $1${NC}"; }
ok()   { echo "${GREEN}✓${NC} $1"; }
warn() { echo "${YELLOW}⚠${NC} $1"; }
err()  { echo "${RED}✗${NC} $1" >&2; }
fatal() { err "$1"; exit 1; }

prompt() {
    local label="$1" default="${2:-}" reply
    if [[ -n "$default" ]]; then
        printf "${BOLD}?${NC} %s [%s]: " "$label" "$default" >&2
    else
        printf "${BOLD}?${NC} %s: " "$label" >&2
    fi
    read -r reply
    echo "${reply:-$default}"
}

prompt_password() {
    local label="$1" reply
    printf "${BOLD}?${NC} %s: " "$label" >&2
    read -rs reply
    echo "" >&2
    echo "$reply"
}

prompt_yesno() {
    local label="$1" default="${2:-y}"
    local prompt_str="[y/N]"
    [[ "$default" =~ ^[Yy] ]] && prompt_str="[Y/n]"
    local reply
    while :; do
        printf "${BOLD}?${NC} %s %s " "$label" "$prompt_str" >&2
        read -r reply
        reply="${reply:-$default}"
        case "$reply" in
            [Yy]|[Yy][Ee][Ss]) echo "y"; return 0 ;;
            [Nn]|[Nn][Oo])     echo "n"; return 0 ;;
            *) echo "  Please answer y or n." >&2 ;;
        esac
    done
}

# ─── Prerequisite checks ─────────────────────────────────────────────────────
check_prereqs() {
    step "Checking prerequisites…"
    command -v docker >/dev/null 2>&1 || fatal "Missing required command: docker"
    command -v curl   >/dev/null 2>&1 || fatal "Missing required command: curl"
    docker compose version >/dev/null 2>&1 \
        || fatal "Docker Compose v2 is required (try: docker compose version)"
    docker info >/dev/null 2>&1 \
        || fatal "Docker daemon does not appear to be running"
    ok "docker, curl and Docker Compose v2 are available"
}

# ─── Splunk admin-API helpers (used only when user opts into auto-setup) ────
SPLUNK_ADMIN_USER=""
SPLUNK_ADMIN_PASS=""

api_call() {
    # $1 = method, $2 = full URL, rest of args passed to curl
    local method="$1" url="$2"
    shift 2
    curl -sk --max-time 30 \
        -u "${SPLUNK_ADMIN_USER}:${SPLUNK_ADMIN_PASS}" \
        -X "$method" "$url" "$@"
}

mgmt_health_check() {
    local mgmt_base="$1" code
    if ! code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
                  -u "${SPLUNK_ADMIN_USER}:${SPLUNK_ADMIN_PASS}" \
                  "${mgmt_base}/services/server/info" 2>/dev/null); then
        code="000"
    fi
    [[ -z "$code" ]] && code="000"
    case "$code" in
        200) ok "Splunk management API reachable at ${mgmt_base}" ;;
        401|403) fatal "Splunk admin auth failed (HTTP ${code}). Check username/password." ;;
        000) fatal "Cannot reach Splunk management API at ${mgmt_base} (network/port?)" ;;
        *)   fatal "Unexpected response from ${mgmt_base} (HTTP ${code})" ;;
    esac
}

create_index_if_missing() {
    local index="$1" mgmt_base="$2" code
    step "Ensuring index '${index}' exists…"
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 15 \
            -u "${SPLUNK_ADMIN_USER}:${SPLUNK_ADMIN_PASS}" \
            "${mgmt_base}/services/data/indexes/${index}")
    case "$code" in
        200) ok "Index '${index}' already exists" ;;
        404)
            local resp
            resp=$(api_call POST "${mgmt_base}/services/data/indexes" \
                       --data-urlencode "name=${index}")
            if echo "$resp" | grep -q "<title>${index}</title>"; then
                ok "Created index '${index}'"
            else
                err "Failed to create index. Splunk responded:"
                echo "$resp" >&2
                exit 1
            fi
            ;;
        401|403) fatal "Splunk admin auth failed (HTTP ${code})" ;;
        *)       fatal "Unexpected response checking index '${index}' (HTTP ${code})" ;;
    esac
}

ensure_hec_enabled() {
    local mgmt_base="$1"
    step "Ensuring HEC global input is enabled (SSL on)…"
    api_call POST "${mgmt_base}/services/data/inputs/http/http" \
        -d "disabled=0" -d "enableSSL=1" >/dev/null
    ok "HEC enabled"
}

create_hec_token() {
    # Echoes the token to stdout. All log output goes to stderr.
    local index="$1" mgmt_base="$2" token_name="thousandeyes-gen"

    step "Creating HEC token '${token_name}' for index '${index}'…" >&2
    api_call DELETE "${mgmt_base}/services/data/inputs/http/${token_name}" \
        >/dev/null 2>&1 || true

    local resp
    resp=$(api_call POST "${mgmt_base}/services/data/inputs/http" \
               --data-urlencode "name=${token_name}" \
               --data-urlencode "index=${index}" \
               --data-urlencode "indexes=${index}" \
               -d "useACK=0")
    local tok
    tok=$(echo "$resp" | sed -n 's/.*<s:key name="token">\([^<]*\)<\/s:key>.*/\1/p' | head -1)
    if [[ -z "$tok" ]]; then
        err "Failed to extract token from Splunk response:"
        echo "$resp" >&2
        exit 1
    fi
    ok "Created HEC token '${token_name}'" >&2
    echo "$tok"
}

# ─── Test that HEC actually accepts our token ────────────────────────────────
test_hec_token() {
    local hec_url="$1" token="$2" verify_ssl="$3" curl_opts=()
    [[ "$verify_ssl" == "n" ]] && curl_opts+=(-k)

    step "Testing HEC at ${hec_url} with provided token…"
    local code
    if ! code=$(curl -s "${curl_opts[@]}" --max-time 15 \
                  -o /dev/null -w "%{http_code}" \
                  -H "Authorization: Splunk ${token}" \
                  "${hec_url%/}/services/collector/health" 2>/dev/null); then
        code="000"
    fi
    [[ -z "$code" ]] && code="000"
    case "$code" in
        200) ok "HEC responded healthy (HTTP 200)" ;;
        400) ok "HEC reachable (HTTP 400 on /health is expected on some versions)" ;;
        401|403) warn "HEC rejected the token (HTTP ${code}). Continuing — double-check token & index later." ;;
        000) warn "Could not reach HEC at ${hec_url} from this host. The container may still reach it (different network). Continuing." ;;
        *)   warn "HEC health check returned HTTP ${code}. Continuing." ;;
    esac
}

# ─── Embedded files ──────────────────────────────────────────────────────────
write_dockerfile() {
    cat > "${INSTALL_DIR}/Dockerfile" <<'DOCKERFILE_EOF'
FROM python:3.12-slim
WORKDIR /app
COPY thousandeyes_generator.py .
ENTRYPOINT ["python3", "thousandeyes_generator.py"]
DOCKERFILE_EOF
}

write_generator_py() {
    cat > "${INSTALL_DIR}/thousandeyes_generator.py" <<'THOUSANDEYES_GENERATOR_PY_EOF'
#!/usr/bin/env python3
"""
Generates realistic fake ThousandEyes data and sends it to Splunk via HEC.

Produces three event types that mirror the ThousandEyes API v7 structure:
  1. Agent-to-Server network tests (latency, loss, jitter)
  2. HTTP Server tests (response time, status codes, availability)
  3. Alert notifications (threshold breaches)

Usage:
  # Continuous mode (default: 1 event every 10s)
  python3 scripts/thousandeyes_generator.py

  # Burst mode: send N events and exit
  python3 scripts/thousandeyes_generator.py --burst 200

  # Custom interval
  python3 scripts/thousandeyes_generator.py --interval 5

  # Custom Splunk target
  python3 scripts/thousandeyes_generator.py --splunk-url https://localhost:8088 --token <hec_token>
"""

import argparse
import errno
import json
import random
import sys
import time
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Test definitions ──

AGENTS = [
    {"agentId": "10001", "agentName": "te-agent-oslo-01", "location": "Oslo, Norway", "countryId": "NO", "lat": 59.91, "lon": 10.75},
    {"agentId": "10002", "agentName": "te-agent-stavanger-01", "location": "Stavanger, Norway", "countryId": "NO", "lat": 58.97, "lon": 5.73},
    {"agentId": "10003", "agentName": "te-agent-bergen-01", "location": "Bergen, Norway", "countryId": "NO", "lat": 60.39, "lon": 5.32},
    {"agentId": "10004", "agentName": "te-agent-london-01", "location": "London, UK", "countryId": "GB", "lat": 51.51, "lon": -0.13},
    {"agentId": "10005", "agentName": "te-agent-frankfurt-01", "location": "Frankfurt, Germany", "countryId": "DE", "lat": 50.11, "lon": 8.68},
    {"agentId": "10006", "agentName": "te-agent-houston-01", "location": "Houston, TX", "countryId": "US", "lat": 29.76, "lon": -95.37},
    {"agentId": "10007", "agentName": "te-agent-singapore-01", "location": "Singapore", "countryId": "SG", "lat": 1.35, "lon": 103.82},
    {"agentId": "10008", "agentName": "te-agent-rio-01", "location": "Rio de Janeiro, Brazil", "countryId": "BR", "lat": -22.91, "lon": -43.17},
]

NETWORK_TESTS = [
    {"testId": "50001", "testName": "WAN - Oslo to Stavanger", "target": "10.20.1.1", "protocol": "ICMP"},
    {"testId": "50002", "testName": "WAN - Oslo to London", "target": "10.30.1.1", "protocol": "TCP"},
    {"testId": "50003", "testName": "Internet - Oslo Breakout", "target": "8.8.8.8", "protocol": "ICMP"},
    {"testId": "50004", "testName": "MPLS - Stavanger to Houston", "target": "10.40.2.1", "protocol": "TCP"},
    {"testId": "50005", "testName": "SD-WAN - Bergen to Frankfurt", "target": "10.50.1.1", "protocol": "TCP"},
    {"testId": "50006", "testName": "Subsea Cable - Stavanger to UK", "target": "10.60.1.1", "protocol": "ICMP"},
    {"testId": "50007", "testName": "Cloud - Azure North Europe", "target": "52.138.224.1", "protocol": "TCP"},
    {"testId": "50008", "testName": "Cloud - AWS eu-west-1", "target": "54.72.0.1", "protocol": "TCP"},
]

HTTP_TESTS = [
    {"testId": "60001", "testName": "SAP Portal", "url": "https://sap.internal.corp/portal", "targetResponseTime": 2000},
    {"testId": "60002", "testName": "ServiceNow ITSM", "url": "https://corp.service-now.com/nav_to.do", "targetResponseTime": 3000},
    {"testId": "60003", "testName": "Office 365 Outlook", "url": "https://outlook.office365.com", "targetResponseTime": 1500},
    {"testId": "60004", "testName": "Teams Meeting Join", "url": "https://teams.microsoft.com/v2", "targetResponseTime": 2000},
    {"testId": "60005", "testName": "Intranet Portal", "url": "https://intranet.corp.local", "targetResponseTime": 1000},
    {"testId": "60006", "testName": "SCADA Gateway", "url": "https://scada-gw.ops.corp:8443/health", "targetResponseTime": 500},
]

ALERT_RULES = [
    {"ruleId": "70001", "ruleName": "High Latency", "expression": "Latency >= 150 ms", "severity": "MAJOR"},
    {"ruleId": "70002", "ruleName": "Packet Loss", "expression": "Loss >= 2%", "severity": "CRITICAL"},
    {"ruleId": "70003", "ruleName": "HTTP 5xx Error", "expression": "Response Code >= 500", "severity": "CRITICAL"},
    {"ruleId": "70004", "ruleName": "Slow Response", "expression": "Response Time >= 3000 ms", "severity": "MINOR"},
    {"ruleId": "70005", "ruleName": "Jitter Threshold", "expression": "Jitter >= 30 ms", "severity": "MAJOR"},
    {"ruleId": "70006", "ruleName": "Path Change", "expression": "Path Trace has changed", "severity": "INFO"},
]

# ── Baselines per test for realistic variation ──

BASELINES = {}
for t in NETWORK_TESTS:
    BASELINES[t["testId"]] = {
        "latency": random.uniform(8, 80),
        "jitter": random.uniform(0.5, 8),
        "loss": 0.0,
    }

for t in HTTP_TESTS:
    BASELINES[t["testId"]] = {
        "responseTime": random.uniform(200, t["targetResponseTime"] * 0.6),
    }


def jittered(base, pct=0.15):
    return max(0, base * random.uniform(1 - pct, 1 + pct))


def maybe_degrade():
    r = random.random()
    if r < 0.03:
        return "major"
    if r < 0.08:
        return "minor"
    return "normal"


def generate_network_event():
    test = random.choice(NETWORK_TESTS)
    agent = random.choice(AGENTS)
    bl = BASELINES[test["testId"]]
    now = datetime.now(timezone.utc)
    round_id = int(now.timestamp())
    degradation = maybe_degrade()

    lat = jittered(bl["latency"])
    jit = jittered(bl["jitter"])
    loss = 0.0

    if degradation == "minor":
        lat *= random.uniform(1.5, 2.5)
        jit *= random.uniform(1.5, 3.0)
        loss = random.uniform(0.1, 1.0)
    elif degradation == "major":
        lat *= random.uniform(3.0, 8.0)
        jit *= random.uniform(3.0, 6.0)
        loss = random.uniform(2.0, 15.0)

    return {
        "type": "agent-to-server",
        "testId": test["testId"],
        "testName": test["testName"],
        "server": f"{test['target']}:443",
        "protocol": test["protocol"],
        "date": now.isoformat(),
        "roundId": round_id,
        "startTime": round_id,
        "endTime": round_id + 60,
        "agent": {
            "agentId": agent["agentId"],
            "agentName": agent["agentName"],
            "agentType": "enterprise",
            "location": agent["location"],
            "countryId": agent["countryId"],
            "coordinates": {
                "latitude": agent["lat"],
                "longitude": agent["lon"],
            },
        },
        "avgLatency": round(lat, 2),
        "minLatency": round(lat * random.uniform(0.6, 0.85), 2),
        "maxLatency": round(lat * random.uniform(1.1, 1.6), 2),
        "jitter": round(jit, 2),
        "loss": round(loss, 2),
        "pathTrace": {
            "numberOfHops": random.randint(4, 14),
            "completed": degradation != "major" or random.random() > 0.3,
        },
        "accountId": "990001",
        "aid": "990001",
    }


def generate_http_event():
    test = random.choice(HTTP_TESTS)
    agent = random.choice(AGENTS)
    bl = BASELINES[test["testId"]]
    now = datetime.now(timezone.utc)
    round_id = int(now.timestamp())
    degradation = maybe_degrade()

    resp_time = jittered(bl["responseTime"])
    status = 200
    availability = 100.0

    if degradation == "minor":
        resp_time *= random.uniform(2.0, 4.0)
        if random.random() < 0.3:
            status = random.choice([502, 503, 504])
            availability = random.uniform(60, 95)
    elif degradation == "major":
        resp_time *= random.uniform(5.0, 15.0)
        status = random.choice([500, 502, 503, 504, 0])
        availability = random.uniform(0, 50) if status == 0 else random.uniform(20, 60)

    dns_time = random.uniform(2, 40)
    connect_time = random.uniform(5, 80)
    ssl_time = random.uniform(10, 120)
    wait_time = resp_time - dns_time - connect_time - ssl_time
    if wait_time < 0:
        wait_time = random.uniform(50, 200)

    return {
        "type": "http-server",
        "testId": test["testId"],
        "testName": test["testName"],
        "url": test["url"],
        "date": now.isoformat(),
        "roundId": round_id,
        "startTime": round_id,
        "endTime": round_id + 60,
        "agent": {
            "agentId": agent["agentId"],
            "agentName": agent["agentName"],
            "agentType": "enterprise",
            "location": agent["location"],
            "countryId": agent["countryId"],
            "coordinates": {
                "latitude": agent["lat"],
                "longitude": agent["lon"],
            },
        },
        "responseTime": round(resp_time, 1),
        "dnsTime": round(dns_time, 1),
        "connectTime": round(connect_time, 1),
        "sslNegotiationTime": round(ssl_time, 1),
        "waitTime": round(max(0, wait_time), 1),
        "responseCode": status,
        "availability": round(availability, 1),
        "throughput": round(random.uniform(500, 12000), 0),
        "accountId": "990001",
        "aid": "990001",
    }


def generate_alert_event():
    rule = random.choice(ALERT_RULES)
    test = random.choice(NETWORK_TESTS + HTTP_TESTS)
    agent = random.choice(AGENTS)
    now = datetime.now(timezone.utc)

    is_cleared = random.random() < 0.4
    triggered_ts = int(now.timestamp()) * 1000 - random.randint(60000, 600000)
    cleared_ts = int(now.timestamp()) * 1000 if is_cleared else None

    return {
        "eventId": f"{rule['ruleId']}-{test['testId']}-{int(now.timestamp())}",
        "eventType": "THOUSANDEYES_ALERT_NOTIFICATION",
        "id": rule["ruleId"],
        "type": "2",
        "accountId": "990001",
        "orgId": "880001",
        "testId": test["testId"],
        "thousandeyes_test_id": test["testId"],
        "test_description": test["testName"],
        "test_type": test.get("testType", "agent-to-server") if "testType" not in test else "http-server",
        "severity_id": {"INFO": "1", "MINOR": "2", "MAJOR": "3", "CRITICAL": "4"}[rule["severity"]],
        "vendor_severity": rule["severity"],
        "app": "THOUSANDEYES",
        "src": test.get("target", test.get("url", "")),
        "signature": rule["ruleName"],
        "alert_type": "Network" if "target" in test else "Http",
        "alert": {
            "id": rule["ruleId"],
            "type": "Network" if "target" in test else "Http",
            "severity": rule["severity"],
            "state": "CLEARED" if is_cleared else "TRIGGERED",
            "test": {"name": test["testName"]},
            "targets": [test.get("target", test.get("url", ""))],
            "rule": {
                "id": rule["ruleId"],
                "name": rule["ruleName"],
                "expression": rule["expression"],
            },
            "triggered": triggered_ts,
            "cleared": cleared_ts,
            "details": [
                {
                    "metricsAtStart": rule["expression"].replace(">=", ":"),
                    "source": {"id": agent["agentId"], "name": agent["agentName"]},
                }
            ],
        },
    }


def _ssl_context(verify_ssl: bool):
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _is_transient_hec_error(exc: BaseException) -> bool:
    """True when Splunk HEC is probably not listening yet (reboot / slow start)."""
    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        if isinstance(r, OSError) and r.errno in (errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH):
            return True
        if isinstance(r, ConnectionResetError):
            return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def wait_for_hec(splunk_url: str, token: str, verify_ssl: bool, interval: float = 5.0, max_wait: float = 600.0) -> None:
    """
    Poll HEC until it answers. Web UI (port 8000) can be healthy before HEC (8088) accepts connections.
    """
    base = splunk_url.rstrip("/")
    health_url = f"{base}/services/collector/health"
    ctx = _ssl_context(verify_ssl)
    deadline = time.monotonic() + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        req = urllib.request.Request(health_url, method="GET")
        req.add_header("Authorization", f"Splunk {token}")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                resp.read()
            print(f"HEC ready at {base} (after {attempt} attempt(s))", file=sys.stderr)
            return
        except urllib.error.HTTPError as e:
            # HEC warming up or not yet bound
            if e.code in (502, 503, 504) or e.code == 404:
                if attempt == 1 or attempt % 6 == 0:
                    print(f"Waiting for HEC at {base}… (HTTP {e.code})", file=sys.stderr)
                time.sleep(interval)
            else:
                print(f"HEC health check failed: HTTP {e.code} {e.reason!r}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            if attempt == 1 or attempt % 6 == 0:
                print(f"Waiting for HEC at {base}… ({e!r})", file=sys.stderr)
            time.sleep(interval)
    print(f"Timeout: HEC did not become ready within {max_wait}s at {base}", file=sys.stderr)
    sys.exit(1)


def send_to_splunk(event, splunk_url, token, index, sourcetype, verify_ssl=False, retries: int = 5, retry_delay: float = 3.0):
    payload = json.dumps({
        "index": index,
        "sourcetype": sourcetype,
        "source": "thousandeyes:api",
        "host": "thousandeyes.cloud",
        "event": event,
    }).encode("utf-8")

    url = f"{splunk_url.rstrip('/')}/services/collector/event"
    ctx = _ssl_context(verify_ssl)

    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Splunk {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if _is_transient_hec_error(e) and attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise


def generate_batch(n):
    events = []
    for _ in range(n):
        r = random.random()
        if r < 0.50:
            ev = generate_network_event()
            st = "thousandeyes:network"
        elif r < 0.85:
            ev = generate_http_event()
            st = "thousandeyes:http"
        else:
            ev = generate_alert_event()
            st = "thousandeyes:alert"
        events.append((ev, st))
    return events


def main():
    parser = argparse.ArgumentParser(description="ThousandEyes fake data generator for Splunk")
    parser.add_argument("--splunk-url", default="https://localhost:8088", help="Splunk HEC URL")
    parser.add_argument("--token", default=None, help="Splunk HEC token")
    parser.add_argument("--index", default="thousandeyes", help="Target Splunk index")
    parser.add_argument("--interval", type=float, default=10, help="Seconds between events in continuous mode")
    parser.add_argument("--burst", type=int, default=0, help="Send N events and exit (0 = continuous)")
    parser.add_argument("--dry-run", action="store_true", help="Print events to stdout instead of sending")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify SSL certificates")
    parser.add_argument(
        "--skip-hec-wait",
        action="store_true",
        help="Do not wait for HEC /health before sending (default: wait up to 10 min after container start)",
    )
    parser.add_argument(
        "--hec-wait-max",
        type=float,
        default=600.0,
        help="Seconds to wait for HEC to accept connections (default: 600)",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.token:
        print("Error: --token required (or use --dry-run)", file=sys.stderr)
        sys.exit(1)

    if args.burst > 0:
        if not args.dry_run and not args.skip_hec_wait:
            wait_for_hec(
                args.splunk_url,
                args.token,
                args.verify_ssl,
                interval=5.0,
                max_wait=float(args.hec_wait_max),
            )
        batch = generate_batch(args.burst)
        sent = 0
        for event, sourcetype in batch:
            if args.dry_run:
                print(json.dumps(event, indent=2))
            else:
                send_to_splunk(event, args.splunk_url, args.token, args.index, sourcetype, args.verify_ssl)
            sent += 1
            if sent % 50 == 0:
                print(f"  sent {sent}/{args.burst}", file=sys.stderr)
        print(f"Done: {sent} events sent to {args.index}", file=sys.stderr)
    else:
        if not args.dry_run and not args.skip_hec_wait:
            wait_for_hec(
                args.splunk_url,
                args.token,
                args.verify_ssl,
                interval=5.0,
                max_wait=float(args.hec_wait_max),
            )
        print(f"Continuous mode: sending events every {args.interval}s to index={args.index}", file=sys.stderr)
        print("Press Ctrl+C to stop", file=sys.stderr)
        count = 0
        try:
            while True:
                batch = generate_batch(random.randint(1, 4))
                for event, sourcetype in batch:
                    if args.dry_run:
                        print(json.dumps(event))
                    else:
                        send_to_splunk(event, args.splunk_url, args.token, args.index, sourcetype, args.verify_ssl)
                    count += 1
                print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] sent {len(batch)} events (total: {count})", file=sys.stderr)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\nStopped. Total events sent: {count}", file=sys.stderr)


if __name__ == "__main__":
    main()
THOUSANDEYES_GENERATOR_PY_EOF
}

write_compose() {
    local splunk_url="$1" hec_token="$2" index="$3" interval="$4"
    local network_mode="$5"      # bridge | external | host
    local external_net="${6:-}"  # only used when network_mode=external

    local -a tail_lines=()

    case "$network_mode" in
        bridge)
            tail_lines+=(
                '    extra_hosts:'
                '      - "host.docker.internal:host-gateway"'
            )
            ;;
        external)
            tail_lines+=(
                '    networks:'
                '      - splunk_external'
            )
            ;;
        host)
            tail_lines+=('    network_mode: host')
            ;;
    esac

    {
        echo "# Generated by thousandeyes-gen-installer.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "# Edit and re-run 'docker compose up -d --build' to apply changes."
        echo "services:"
        echo "  ${CONTAINER_NAME}:"
        echo "    build:"
        echo "      context: ."
        echo "      dockerfile: Dockerfile"
        echo "    image: ${CONTAINER_NAME}:local"
        echo "    container_name: ${CONTAINER_NAME}"
        echo "    hostname: thousandeyes-gen"
        echo "    restart: unless-stopped"
        echo "    command:"
        echo "      - --splunk-url"
        echo "      - \"${splunk_url}\""
        echo "      - --token"
        echo "      - \"\${SPLUNK_HEC_TOKEN}\""
        echo "      - --index"
        echo "      - \"${index}\""
        echo "      - --interval"
        echo "      - \"${interval}\""
        for line in "${tail_lines[@]}"; do
            echo "$line"
        done

        if [[ "$network_mode" == "external" ]]; then
            echo ""
            echo "networks:"
            echo "  splunk_external:"
            echo "    name: ${external_net}"
            echo "    external: true"
        fi
    } > "${INSTALL_DIR}/docker-compose.yml"

    {
        echo "# Generated by thousandeyes-gen-installer.sh"
        echo "# Keep this file out of version control — it contains the HEC token."
        echo "SPLUNK_HEC_TOKEN=${hec_token}"
    } > "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
}

write_readme() {
    cat > "${INSTALL_DIR}/README.md" <<'README_EOF'
# thousandeyes-gen

Fake ThousandEyes data generator → Splunk HEC. Installed by
`thousandeyes-gen-installer.sh`.

## Files in this directory

- `Dockerfile` — Python 3.12 slim base, copies the generator in.
- `thousandeyes_generator.py` — the actual event generator.
- `docker-compose.yml` — wires up the container with your Splunk URL.
- `.env` — holds the HEC token (kept at chmod 600).

## Useful commands (run from this directory)

```bash
docker compose up -d --build      # (re)build and start
docker compose logs -f            # follow logs
docker compose down               # stop and remove
docker compose restart            # restart with new env (e.g. token rotated)
```

## Changing settings

Edit `docker-compose.yml` (URL, index, interval) and run
`docker compose up -d --build`. To rotate the HEC token, edit `.env` and run
`docker compose up -d`.

## Re-running the installer

You can run the installer again at any time with `--install`. It will overwrite
the files in this directory and rebuild the container.
README_EOF
}

# ─── Install / start / stop helpers ──────────────────────────────────────────
build_and_start() {
    step "Building image and starting container…"
    (cd "${INSTALL_DIR}" && docker compose up -d --build)
    ok "Container is up"
}

stop_and_remove() {
    if [[ -d "${INSTALL_DIR}" && -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
        step "Stopping container…"
        (cd "${INSTALL_DIR}" && docker compose down) || true
        ok "Container removed"
    else
        warn "No install dir at ${INSTALL_DIR} — nothing to stop"
    fi
    if [[ -d "${INSTALL_DIR}" ]]; then
        if [[ "$(prompt_yesno "Also delete ${INSTALL_DIR}?" "n")" == "y" ]]; then
            rm -rf "${INSTALL_DIR}"
            ok "Removed ${INSTALL_DIR}"
        fi
    fi
}

# ─── Main install flow ───────────────────────────────────────────────────────
do_install() {
    banner "thousandeyes-gen — installer"
    check_prereqs

    info "Install directory: ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"

    echo ""
    banner "Step 1 / 4 — Splunk connection"

    cat <<EOF >&2

How is the generator container going to reach your Splunk?

  1) Splunk-in-Docker on this same host, with HEC port published to the host
     (most common). The generator will connect to host.docker.internal:<port>.
  2) Splunk-in-Docker on this host, where you want the generator to join the
     same Docker network. You'll be asked for the Docker network name and the
     Splunk container's name/hostname on that network.
  3) Splunk on a different machine (use a hostname or IP).
  4) Use the host's network namespace (Linux only — generator binds 'localhost').

EOF
    local choice
    choice=$(prompt "Choose [1/2/3/4]" "1")

    local splunk_host hec_port use_https network_mode external_net=""
    case "$choice" in
        1)
            network_mode="bridge"
            splunk_host="host.docker.internal"
            hec_port=$(prompt "Splunk HEC port (published on host)" "8088")
            ;;
        2)
            network_mode="external"
            external_net=$(prompt "Existing Docker network name" "")
            [[ -z "$external_net" ]] && fatal "Docker network name is required"
            if ! docker network inspect "$external_net" >/dev/null 2>&1; then
                fatal "Docker network '${external_net}' not found. Check 'docker network ls'."
            fi
            splunk_host=$(prompt "Splunk container name/hostname on '${external_net}'" "splunk")
            hec_port=$(prompt "Splunk HEC port (inside container, usually 8088)" "8088")
            ;;
        3)
            network_mode="bridge"
            splunk_host=$(prompt "Splunk hostname or IP" "")
            [[ -z "$splunk_host" ]] && fatal "Splunk hostname/IP is required"
            hec_port=$(prompt "Splunk HEC port" "8088")
            ;;
        4)
            network_mode="host"
            splunk_host=$(prompt "Splunk hostname (use 'localhost' if Splunk runs on this host)" "localhost")
            hec_port=$(prompt "Splunk HEC port" "8088")
            ;;
        *) fatal "Invalid choice: $choice" ;;
    esac

    use_https=$(prompt_yesno "Use HTTPS for HEC?" "y")
    local scheme="http"
    [[ "$use_https" == "y" ]] && scheme="https"
    local splunk_hec_url="${scheme}://${splunk_host}:${hec_port}"
    info "HEC URL → ${splunk_hec_url}"

    local verify_ssl
    if [[ "$use_https" == "y" ]]; then
        verify_ssl=$(prompt_yesno "Verify SSL certificate? (Splunk default cert is self-signed → answer N)" "n")
    else
        verify_ssl="n"
    fi

    echo ""
    banner "Step 2 / 4 — Index and HEC token"
    local index_name
    index_name=$(prompt "Splunk index name" "thousandeyes")

    local have_token hec_token=""
    have_token=$(prompt_yesno "Do you already have a HEC token for this index?" "n")

    if [[ "$have_token" == "y" ]]; then
        hec_token=$(prompt_password "Paste HEC token (UUID format)")
        [[ -z "$hec_token" ]] && fatal "Empty HEC token"
    else
        echo ""
        info "We'll create the index and a HEC token for you using Splunk's admin API."
        info "This needs the management port (default 8089) and admin credentials."

        local mgmt_port mgmt_host mgmt_scheme="https"
        if [[ "$network_mode" == "external" ]]; then
            mgmt_host="$splunk_host"
            mgmt_port=$(prompt "Splunk management port (inside container)" "8089")
        else
            # Admin API is reachable from this host, not from the generator container
            local default_mgmt_host="localhost"
            [[ "$choice" == "3" ]] && default_mgmt_host="$splunk_host"
            mgmt_host=$(prompt "Splunk management hostname/IP (reachable from THIS host)" "$default_mgmt_host")
            mgmt_port=$(prompt "Splunk management port" "8089")
        fi
        local mgmt_base="${mgmt_scheme}://${mgmt_host}:${mgmt_port}"
        SPLUNK_ADMIN_USER=$(prompt "Splunk admin username" "admin")
        SPLUNK_ADMIN_PASS=$(prompt_password "Splunk admin password")

        if [[ "$network_mode" == "external" ]]; then
            warn "Management port may not be reachable from THIS host (you joined an internal network)."
            warn "If the next call fails, expose port ${mgmt_port} on your Splunk container or paste a token manually."
        fi

        mgmt_health_check "$mgmt_base"
        ensure_hec_enabled  "$mgmt_base"
        create_index_if_missing "$index_name" "$mgmt_base"
        hec_token=$(create_hec_token "$index_name" "$mgmt_base")
    fi

    echo ""
    banner "Step 3 / 4 — Generator settings"
    local interval
    interval=$(prompt "Event interval seconds (lower = more events)" "10")

    # Light sanity test from this host (best-effort)
    if [[ "$network_mode" != "external" ]]; then
        test_hec_token "$splunk_hec_url" "$hec_token" "$verify_ssl"
    else
        info "Skipping host-side HEC test (target only reachable from inside Docker network ${external_net})"
    fi

    echo ""
    banner "Step 4 / 4 — Writing files and starting container"
    write_dockerfile
    write_generator_py
    write_compose "$splunk_hec_url" "$hec_token" "$index_name" "$interval" \
                  "$network_mode" "$external_net"
    write_readme
    ok "Wrote files to ${INSTALL_DIR}"

    if [[ "$(prompt_yesno "Build the image and start the container now?" "y")" == "y" ]]; then
        build_and_start
        sleep 2
        info "Container status:"
        docker ps --filter "name=^/${CONTAINER_NAME}$" \
                  --format "  {{.Names}}\t{{.Status}}\t{{.Image}}" || true
    else
        info "Skipped start. To start later: cd ${INSTALL_DIR} && docker compose up -d --build"
    fi

    echo ""
    banner "Done"
    cat <<EOF >&2
Installed at:   ${INSTALL_DIR}
HEC URL:        ${splunk_hec_url}
Index:          ${index_name}
Interval:       ${interval}s
Network mode:   ${network_mode}${external_net:+ (network=${external_net})}

Verify in Splunk:
    index=${index_name} | head 20

Useful commands:
    cd ${INSTALL_DIR}
    docker compose logs -f      # follow generator logs
    docker compose restart      # restart container
    docker compose down         # stop and remove

Re-run this installer with --uninstall to clean up.
EOF
}

do_uninstall() {
    banner "thousandeyes-gen — uninstall"
    stop_and_remove
}

do_status() {
    if docker ps -a --filter "name=^/${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q .; then
        docker ps -a --filter "name=^/${CONTAINER_NAME}$" \
                --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    else
        warn "Container '${CONTAINER_NAME}' not found"
    fi
}

do_logs() {
    docker logs -f "${CONTAINER_NAME}"
}

usage() {
    # Print only the leading comment block (lines starting with '#').
    awk 'NR==1{next} /^[^#]/{exit} {print}' "$0"
}

# ─── Entry point ─────────────────────────────────────────────────────────────
main() {
    local cmd="${1:---install}"
    case "$cmd" in
        --install|install|"")  do_install ;;
        --uninstall|uninstall) do_uninstall ;;
        --status|status)       do_status ;;
        --logs|logs)           do_logs ;;
        --help|-h|help)        usage ;;
        *) usage; exit 1 ;;
    esac
}

main "${1:-}"
