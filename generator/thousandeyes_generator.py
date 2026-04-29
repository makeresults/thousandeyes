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
    {"testId": "50009", "testName": "Cloud - AWS us-east-1", "target": "3.5.140.1", "protocol": "TCP"},
    {"testId": "50010", "testName": "Cloud - Azure East US", "target": "20.42.0.1", "protocol": "TCP"},
    {"testId": "50011", "testName": "Cloud - GCP europe-north1", "target": "35.228.0.1", "protocol": "TCP"},
    {"testId": "50012", "testName": "Edge - Cloudflare Anycast", "target": "1.1.1.1", "protocol": "ICMP"},
]

HTTP_TESTS = [
    {"testId": "60001", "testName": "SAP Portal", "url": "https://sap.internal.corp/portal", "targetResponseTime": 2000},
    {"testId": "60002", "testName": "ServiceNow ITSM", "url": "https://corp.service-now.com/nav_to.do", "targetResponseTime": 3000},
    {"testId": "60003", "testName": "Office 365 Outlook", "url": "https://outlook.office365.com", "targetResponseTime": 1500},
    {"testId": "60004", "testName": "Teams Meeting Join", "url": "https://teams.microsoft.com/v2", "targetResponseTime": 2000},
    {"testId": "60005", "testName": "Intranet Portal", "url": "https://intranet.corp.local", "targetResponseTime": 1000},
    {"testId": "60006", "testName": "SCADA Gateway", "url": "https://scada-gw.ops.corp:8443/health", "targetResponseTime": 500},
    {"testId": "60007", "testName": "S3 - Public Object", "url": "https://s3.eu-west-1.amazonaws.com/health-bucket/check.txt", "targetResponseTime": 1500},
    {"testId": "60008", "testName": "Azure Front Door CDN", "url": "https://corp-app.azurefd.net/health", "targetResponseTime": 800},
    {"testId": "60009", "testName": "GCP Cloud Run API", "url": "https://backend-svc-ew.a.run.app/health", "targetResponseTime": 1200},
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
