#!/usr/bin/env python3
"""
Generates synthetic ThousandEyes data and ships it to Splunk via HEC, in a
format that is 100% compatible with the official Cisco ThousandEyes App for
Splunk (TA: ta_cisco_thousandeyes).

Reverse-engineered against TA v0.6.0 — see TA_REQUIREMENTS.md in the repo
root for the full mapping of sourcetypes, fields and metric_name namespace.

Six event streams are produced (matching the TA's input modes):
  1. cisco:thousandeyes:metric    — Tests Stream Metrics
                                    (network/http/dns/ftp/sip/rtp/page-load/api/web-transactions/bgp)
  2. cisco:thousandeyes:trace     — Tests Stream Traces
                                    (page-load, web-transactions, api)
  3. cisco:thousandeyes:path-vis  — Path Visualization (per-test hop trace)
  4. cisco:thousandeyes:event     — TE platform events (outages etc.)
  5. cisco:thousandeyes:activity  — User audit-log activity events
  6. cisco:thousandeyes:alerts    — Alert webhook notifications

Numerical measurements are emitted as event keys with the literal prefix
``metric_name:`` (e.g. ``"metric_name:network.latency": 12.5``) so that the
TA's data model — which uses ``tstats summariesonly avg(Test_Metrics.metric_name:network.latency)`` —
populates correctly.

Usage
-----
::

  # Continuous (default)
  python3 thousandeyes_generator.py --token <hec-token>

  # Burst then exit
  python3 thousandeyes_generator.py --token <hec-token> --burst 200

  # Backfill ‑24h on startup, then continue continuously
  python3 thousandeyes_generator.py --token <hec-token> --backfill 24

  # Restrict to specific streams (comma-separated)
  python3 thousandeyes_generator.py --token <hec-token> \\
      --streams metric,alerts,path-vis

  # Dry-run (print events to stdout, do not POST)
  python3 thousandeyes_generator.py --dry-run

  # Legacy mode: emit the pre-TA event format (sourcetype thousandeyes:network etc.)
  python3 thousandeyes_generator.py --token <hec-token> --legacy
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import random
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants — sourcetypes, source, host (matched to TA v0.6.0)
# ---------------------------------------------------------------------------
TE_SOURCE_STREAM = "cisco:thousandeyes:stream"
TE_SOURCE_WEBHOOK = "cisco:thousandeyes:webhook"
TE_HOST = "thousandeyesotel"

ST_METRIC = "cisco:thousandeyes:metric"
ST_TRACE = "cisco:thousandeyes:trace"
ST_PATH_VIS = "cisco:thousandeyes:path-vis"
ST_EVENT = "cisco:thousandeyes:event"
ST_ACTIVITY = "cisco:thousandeyes:activity"
ST_ALERTS = "cisco:thousandeyes:alerts"

# Distribution of stream types in continuous mode (must sum to ~1.0).
DEFAULT_MIX = {
    "metric": 0.62,
    "trace": 0.08,
    "path-vis": 0.04,
    "event": 0.02,
    "activity": 0.04,
    "alerts": 0.20,
}

ACCOUNT_ID = "990001"
ORG_ID = "880001"
ACCOUNT_GROUP_NAME = "Production"
STREAM_ID = "stream-9da7c12b"
TENANT_DOMAIN = "app.thousandeyes.com"

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
AGENTS = [
    {"agentId": "10001", "agentName": "te-agent-oslo-01",
     "location": "Oslo, Norway", "countryId": "NO",
     "lat": 59.91, "lon": 10.75, "agentType": "enterprise"},
    {"agentId": "10002", "agentName": "te-agent-stavanger-01",
     "location": "Stavanger, Norway", "countryId": "NO",
     "lat": 58.97, "lon": 5.73, "agentType": "enterprise"},
    {"agentId": "10003", "agentName": "te-agent-bergen-01",
     "location": "Bergen, Norway", "countryId": "NO",
     "lat": 60.39, "lon": 5.32, "agentType": "enterprise"},
    {"agentId": "10004", "agentName": "te-agent-london-01",
     "location": "London, UK", "countryId": "GB",
     "lat": 51.51, "lon": -0.13, "agentType": "cloud"},
    {"agentId": "10005", "agentName": "te-agent-frankfurt-01",
     "location": "Frankfurt, Germany", "countryId": "DE",
     "lat": 50.11, "lon": 8.68, "agentType": "cloud"},
    {"agentId": "10006", "agentName": "te-agent-houston-01",
     "location": "Houston, TX", "countryId": "US",
     "lat": 29.76, "lon": -95.37, "agentType": "cloud"},
    {"agentId": "10007", "agentName": "te-agent-singapore-01",
     "location": "Singapore", "countryId": "SG",
     "lat": 1.35, "lon": 103.82, "agentType": "cloud"},
    {"agentId": "10008", "agentName": "te-agent-rio-01",
     "location": "Rio de Janeiro, Brazil", "countryId": "BR",
     "lat": -22.91, "lon": -43.17, "agentType": "cloud"},
    {"agentId": "20001", "agentName": "endpoint-laptop-no-001",
     "location": "Oslo, Norway", "countryId": "NO",
     "lat": 59.91, "lon": 10.75, "agentType": "endpoint"},
    {"agentId": "20002", "agentName": "endpoint-laptop-uk-001",
     "location": "London, UK", "countryId": "GB",
     "lat": 51.51, "lon": -0.13, "agentType": "endpoint"},
]

NETWORK_TESTS = [
    {"testId": "50001", "testName": "WAN - Oslo to Stavanger",
     "testType": "agent-to-server", "target": "10.20.1.1", "port": 443,
     "transport": "icmp", "domain": "cea"},
    {"testId": "50002", "testName": "WAN - Oslo to London",
     "testType": "agent-to-server", "target": "10.30.1.1", "port": 443,
     "transport": "tcp", "domain": "cea"},
    {"testId": "50003", "testName": "Internet - Oslo Breakout",
     "testType": "agent-to-server", "target": "8.8.8.8", "port": 53,
     "transport": "icmp", "domain": "cea"},
    {"testId": "50004", "testName": "MPLS - Stavanger to Houston",
     "testType": "agent-to-server", "target": "10.40.2.1", "port": 443,
     "transport": "tcp", "domain": "cea"},
    {"testId": "50005", "testName": "SD-WAN - Bergen to Frankfurt",
     "testType": "agent-to-agent", "target": "10.50.1.1", "port": 443,
     "transport": "tcp", "domain": "cea"},
    {"testId": "50006", "testName": "Subsea Cable - Stavanger to UK",
     "testType": "agent-to-server", "target": "10.60.1.1", "port": 443,
     "transport": "icmp", "domain": "cea"},
    {"testId": "50007", "testName": "Cloud - Azure North Europe",
     "testType": "agent-to-server", "target": "52.138.224.1", "port": 443,
     "transport": "tcp", "domain": "cea"},
    {"testId": "50008", "testName": "Cloud - AWS eu-west-1",
     "testType": "agent-to-server", "target": "54.72.0.1", "port": 443,
     "transport": "tcp", "domain": "cea"},
]

HTTP_TESTS = [
    {"testId": "60001", "testName": "SAP Portal", "testType": "http-server",
     "url": "https://sap.internal.corp/portal",
     "target": "sap.internal.corp", "port": 443,
     "targetResponseTime": 2000, "domain": "cea"},
    {"testId": "60002", "testName": "ServiceNow ITSM", "testType": "http-server",
     "url": "https://corp.service-now.com/nav_to.do",
     "target": "corp.service-now.com", "port": 443,
     "targetResponseTime": 3000, "domain": "cea"},
    {"testId": "60003", "testName": "Office 365 Outlook", "testType": "http-server",
     "url": "https://outlook.office365.com",
     "target": "outlook.office365.com", "port": 443,
     "targetResponseTime": 1500, "domain": "cea"},
    {"testId": "60004", "testName": "Teams Meeting Join", "testType": "http-server",
     "url": "https://teams.microsoft.com/v2",
     "target": "teams.microsoft.com", "port": 443,
     "targetResponseTime": 2000, "domain": "cea"},
    {"testId": "60005", "testName": "Intranet Portal", "testType": "http-server",
     "url": "https://intranet.corp.local",
     "target": "intranet.corp.local", "port": 443,
     "targetResponseTime": 1000, "domain": "cea"},
    {"testId": "60006", "testName": "SCADA Gateway", "testType": "http-server",
     "url": "https://scada-gw.ops.corp:8443/health",
     "target": "scada-gw.ops.corp", "port": 8443,
     "targetResponseTime": 500, "domain": "cea"},
]

PAGELOAD_TESTS = [
    {"testId": "61001", "testName": "Office 365 Portal Load",
     "testType": "page-load", "url": "https://portal.office.com",
     "target": "portal.office.com", "port": 443,
     "targetResponseTime": 3000, "domain": "cea"},
    {"testId": "61002", "testName": "Salesforce Lightning",
     "testType": "page-load", "url": "https://corp.lightning.force.com",
     "target": "corp.lightning.force.com", "port": 443,
     "targetResponseTime": 4000, "domain": "cea"},
    {"testId": "61003", "testName": "Workday HCM",
     "testType": "page-load", "url": "https://corp.workday.com/home",
     "target": "corp.workday.com", "port": 443,
     "targetResponseTime": 3500, "domain": "cea"},
]

TRANSACTION_TESTS = [
    # NB: TA's eventtypes.conf uses "web-transaction" (singular) while
    # constants.py uses "web-transactions" (plural). Application dashboard
    # filters with the singular form, so we emit that.
    {"testId": "62001", "testName": "SAP Login Flow",
     "testType": "web-transaction", "url": "https://sap.internal.corp/portal",
     "target": "sap.internal.corp", "port": 443,
     "targetResponseTime": 6000, "domain": "cea"},
    {"testId": "62002", "testName": "ServiceNow Ticket Flow",
     "testType": "web-transaction", "url": "https://corp.service-now.com/login",
     "target": "corp.service-now.com", "port": 443,
     "targetResponseTime": 5000, "domain": "cea"},
]

API_TESTS = [
    {"testId": "63001", "testName": "Payments API Health",
     "testType": "api", "url": "https://api.payments.corp/v1/health",
     "target": "api.payments.corp", "port": 443,
     "targetResponseTime": 800, "domain": "cea"},
    {"testId": "63002", "testName": "Identity API",
     "testType": "api", "url": "https://api.identity.corp/v2/whoami",
     "target": "api.identity.corp", "port": 443,
     "targetResponseTime": 600, "domain": "cea"},
    {"testId": "63003", "testName": "Inventory API",
     "testType": "api", "url": "https://api.inventory.corp/v1/stock",
     "target": "api.inventory.corp", "port": 443,
     "targetResponseTime": 1000, "domain": "cea"},
]

DNS_TESTS = [
    {"testId": "64001", "testName": "DNS - Google",
     "testType": "dns-server", "target": "8.8.8.8", "port": 53,
     "transport": "udp", "query": "outlook.office365.com", "domain": "cea"},
    {"testId": "64002", "testName": "DNS - Cloudflare",
     "testType": "dns-server", "target": "1.1.1.1", "port": 53,
     "transport": "udp", "query": "teams.microsoft.com", "domain": "cea"},
]

VOICE_TESTS = [
    {"testId": "65001", "testName": "Webex Calling - EU",
     "testType": "rtp", "target": "rtp-eu.webex.com", "port": 5060,
     "transport": "udp", "domain": "cea"},
    {"testId": "65002", "testName": "Teams Voice Edge",
     "testType": "rtp", "target": "voice.teams.microsoft.com", "port": 5060,
     "transport": "udp", "domain": "cea"},
]

BGP_MONITORS = [
    {"monitorId": "30001", "monitorName": "AT&T (US)",
     "location": "Dallas, TX", "asn": "AS7018"},
    {"monitorId": "30002", "monitorName": "GTT (EU)",
     "location": "Frankfurt, DE", "asn": "AS3257"},
    {"monitorId": "30003", "monitorName": "NTT (APAC)",
     "location": "Tokyo, JP", "asn": "AS2914"},
]

BGP_PREFIXES = [
    {"testId": "66001", "testName": "Microsoft Azure (52.138.0.0/16)",
     "prefix": "52.138.0.0/16", "asn": "AS8075", "domain": "cea"},
    {"testId": "66002", "testName": "AWS (54.72.0.0/15)",
     "prefix": "54.72.0.0/15", "asn": "AS16509", "domain": "cea"},
]

ALERT_RULES = [
    {"ruleId": "70001", "ruleName": "High Latency",
     "expression": "Latency >= 150 ms", "severity": "MAJOR",
     "alertType": "Network Outage"},
    {"ruleId": "70002", "ruleName": "Packet Loss",
     "expression": "Loss >= 2%", "severity": "CRITICAL",
     "alertType": "Network Outage"},
    {"ruleId": "70003", "ruleName": "HTTP 5xx Error",
     "expression": "Response Code >= 500", "severity": "CRITICAL",
     "alertType": "HTTP Server Error"},
    {"ruleId": "70004", "ruleName": "Slow Response",
     "expression": "Response Time >= 3000 ms", "severity": "MINOR",
     "alertType": "HTTP Server Performance"},
    {"ruleId": "70005", "ruleName": "Jitter Threshold",
     "expression": "Jitter >= 30 ms", "severity": "MAJOR",
     "alertType": "Voice Quality"},
    {"ruleId": "70006", "ruleName": "Path Change",
     "expression": "Path Trace has changed", "severity": "INFO",
     "alertType": "Path Visualization"},
    {"ruleId": "70007", "ruleName": "BGP Reachability",
     "expression": "BGP reachability < 95%", "severity": "CRITICAL",
     "alertType": "BGP Reachability"},
]

PLATFORM_EVENT_TEMPLATES = [
    {"type": "outage", "typeName": "Service Outage",
     "title": "Cloud provider region unreachable", "severity": "high"},
    {"type": "interface", "typeName": "Interface Issue",
     "title": "Path change detected on backbone", "severity": "medium"},
    {"type": "service", "typeName": "Service Issue",
     "title": "ISP performance degradation", "severity": "low"},
]

ACTIVITY_EVENTS = [
    "test.created", "test.updated", "test.deleted",
    "alert_rule.updated", "user.login", "user.logout",
    "agent.added", "agent.removed",
    "integration.created", "integration.updated",
]

USERS = [
    {"id": "user-001", "name": "alice@corp.com", "email": "alice@corp.com",
     "address": "10.0.0.5"},
    {"id": "user-002", "name": "bob@corp.com", "email": "bob@corp.com",
     "address": "10.0.0.6"},
    {"id": "user-003", "name": "ops-svc@corp.com",
     "email": "ops-svc@corp.com", "address": "10.0.0.10"},
]

# Per-test baselines so degradation looks like a deviation, not noise.
BASELINES: dict = {}
for _t in NETWORK_TESTS:
    BASELINES[_t["testId"]] = {
        "latency": random.uniform(8, 80),
        "jitter": random.uniform(0.5, 8),
        "loss": 0.0,
    }
for _t in HTTP_TESTS + PAGELOAD_TESTS + TRANSACTION_TESTS + API_TESTS:
    BASELINES[_t["testId"]] = {
        "responseTime": random.uniform(200, _t["targetResponseTime"] * 0.6),
    }
for _t in DNS_TESTS:
    BASELINES[_t["testId"]] = {"lookup": random.uniform(5, 80)}
for _t in VOICE_TESTS:
    BASELINES[_t["testId"]] = {
        "latency": random.uniform(20, 90),
        "jitter": random.uniform(1, 10),
        "loss": 0.0,
        "mos": random.uniform(4.1, 4.5),
    }
for _t in BGP_PREFIXES:
    BASELINES[_t["testId"]] = {
        "reachability": random.uniform(98, 100),
        "updates": random.uniform(0, 4),
        "path_changes": 0,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def jittered(base: float, pct: float = 0.15) -> float:
    return max(0.0, base * random.uniform(1 - pct, 1 + pct))


def maybe_degrade() -> str:
    r = random.random()
    if r < 0.03:
        return "major"
    if r < 0.10:
        return "minor"
    return "normal"


def hex_id(nbytes: int) -> str:
    return secrets.token_hex(nbytes)


def permalink(test_id: str, agent_id: str | None = None) -> str:
    base = f"https://{TENANT_DOMAIN}/network-app-synthetics/views/?__a={ACCOUNT_ID}&testId={test_id}"
    if agent_id:
        base += f"&agentId={agent_id}"
    return base


def base_test_attrs(test: dict, agent: dict, target_agent: dict | None = None) -> dict:
    """Common ThousandEyes attribute set on metric/trace events."""
    attrs = {
        "thousandeyes.account.id": ACCOUNT_ID,
        "thousandeyes.account.name": ACCOUNT_GROUP_NAME,
        "thousandeyes.stream.id": STREAM_ID,
        "thousandeyes.test.id": test["testId"],
        "thousandeyes.test.name": test["testName"],
        "thousandeyes.test.type": test["testType"],
        "thousandeyes.test.domain": test.get("domain", "cea"),
        "thousandeyes.source.agent.id": agent["agentId"],
        "thousandeyes.source.agent.name": agent["agentName"],
        "thousandeyes.source.agent.location": agent["location"],
        "thousandeyes.permalink": permalink(test["testId"], agent["agentId"]),
    }
    if target_agent is not None:
        attrs["thousandeyes.target.agent.id"] = target_agent["agentId"]
        attrs["thousandeyes.target.agent.name"] = target_agent["agentName"]
    return attrs


def aid_orgid_block() -> dict:
    return {"aid": ACCOUNT_ID, "accountGroupName": ACCOUNT_GROUP_NAME}


# ---------------------------------------------------------------------------
# Stream emitters — each returns dict {sourcetype, source, host, time, event}
# ---------------------------------------------------------------------------
def _envelope(sourcetype: str, payload: dict, *,
              ts: datetime, host: str = TE_HOST,
              source: str = TE_SOURCE_STREAM) -> dict:
    return {
        "time": ts.timestamp(),
        "host": host,
        "source": source,
        "sourcetype": sourcetype,
        "event": payload,
    }


def emit_metric_network(ts: datetime) -> dict:
    test = random.choice(NETWORK_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()

    lat = jittered(bl["latency"])
    jit = jittered(bl["jitter"])
    loss = 0.0
    if deg == "minor":
        lat *= random.uniform(1.5, 2.5)
        jit *= random.uniform(1.5, 3.0)
        loss = random.uniform(0.1, 1.0)
    elif deg == "major":
        lat *= random.uniform(3.0, 8.0)
        jit *= random.uniform(3.0, 6.0)
        loss = random.uniform(2.0, 15.0)

    target_agent = None
    if test["testType"] == "agent-to-agent":
        # pick a different agent as target
        candidates = [a for a in AGENTS if a["agentId"] != agent["agentId"]
                      and a["agentType"] != "endpoint"]
        target_agent = random.choice(candidates)

    payload = {
        **base_test_attrs(test, agent, target_agent),
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "network.transport": test.get("transport", "tcp"),
        "metric_name:network.latency": round(lat, 2),
        "metric_name:network.loss": round(loss, 3),
        "metric_name:network.jitter": round(jit, 2),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_http(ts: datetime) -> dict:
    test = random.choice(HTTP_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()

    rt = jittered(bl["responseTime"])
    status = 200
    avail = 100.0
    if deg == "minor":
        rt *= random.uniform(2.0, 4.0)
        if random.random() < 0.3:
            status = random.choice([502, 503, 504])
            avail = random.uniform(60, 95)
    elif deg == "major":
        rt *= random.uniform(5.0, 15.0)
        status = random.choice([500, 502, 503, 504])
        avail = random.uniform(20, 60)

    throughput = random.uniform(50_000, 4_000_000)  # bytes/s

    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "http.request.method": "GET",
        "http.response.status_code": status,
        "url.full": test["url"],
        # TA expects http duration in seconds; multiplied to ms via EVAL
        "metric_name:http.client.request.duration": round(rt / 1000.0, 4),
        "metric_name:http.server.request.availability": round(avail, 2),
        "metric_name:http.server.throughput": round(throughput, 1),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_pageload(ts: datetime) -> dict:
    test = random.choice(PAGELOAD_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    duration = jittered(bl["responseTime"])
    completion = 100.0
    if deg == "minor":
        duration *= random.uniform(1.8, 3.0)
        completion = random.uniform(85, 99)
    elif deg == "major":
        duration *= random.uniform(4.0, 10.0)
        completion = random.uniform(30, 80)
    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "url.full": test["url"],
        "metric_name:web.page_load.duration": round(duration, 1),
        "metric_name:web.page_load.completion": round(completion, 2),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_transaction(ts: datetime) -> dict:
    test = random.choice(TRANSACTION_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    duration = jittered(bl["responseTime"])
    completion = 100.0
    if deg == "minor":
        duration *= random.uniform(1.5, 2.5)
        completion = random.uniform(80, 99)
    elif deg == "major":
        duration *= random.uniform(3.0, 6.0)
        completion = random.uniform(20, 60)
    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "url.full": test["url"],
        "metric_name:web.transaction.duration": round(duration, 1),
        "metric_name:web.transaction.completion": round(completion, 2),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_api(ts: datetime) -> dict:
    test = random.choice(API_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    duration = jittered(bl["responseTime"])
    completion = 100.0
    if deg == "minor":
        duration *= random.uniform(1.5, 2.5)
        completion = random.uniform(85, 99)
    elif deg == "major":
        duration *= random.uniform(3.0, 5.0)
        completion = random.uniform(40, 70)
    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "url.full": test["url"],
        "metric_name:api.step.duration": round(duration, 1),
        "metric_name:api.step.completion": round(completion, 2),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_dns(ts: datetime) -> dict:
    test = random.choice(DNS_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    lookup = jittered(bl["lookup"])
    avail = 100.0
    valid = 100.0
    if deg == "minor":
        lookup *= random.uniform(2.0, 4.0)
        avail = random.uniform(70, 95)
    elif deg == "major":
        lookup *= random.uniform(5.0, 10.0)
        avail = random.uniform(20, 60)
        valid = random.uniform(50, 85)
    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 53),
        "network.transport": test.get("transport", "udp"),
        "dns.question.name": test["query"],
        "metric_name:dns.lookup.duration": round(lookup, 2),
        "metric_name:dns.lookup.availability": round(avail, 2),
        "metric_name:dns.lookup.validity": round(valid, 2),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_voice(ts: datetime) -> dict:
    test = random.choice(VOICE_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    mos = jittered(bl["mos"], pct=0.05)
    if deg == "minor":
        mos -= random.uniform(0.4, 0.9)
    elif deg == "major":
        mos -= random.uniform(1.0, 2.0)
    mos = max(1.0, min(5.0, mos))
    duration = jittered(60.0)
    payload = {
        **base_test_attrs(test, agent),
        "server.address": test["target"],
        "server.port": test.get("port", 5060),
        "network.transport": test.get("transport", "udp"),
        "metric_name:rtp.client.request.mos": round(mos, 2),
        "metric_name:rtp.client.request.duration": round(duration / 1000.0, 4),
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_metric_bgp(ts: datetime) -> dict:
    test = random.choice(BGP_PREFIXES)
    monitor = random.choice(BGP_MONITORS)
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    reach = jittered(bl["reachability"], pct=0.02)
    updates = bl["updates"] + random.uniform(0, 6)
    path_changes = 0
    if deg == "minor":
        reach -= random.uniform(2, 8)
        updates += random.uniform(8, 20)
        path_changes = random.randint(1, 3)
    elif deg == "major":
        reach -= random.uniform(15, 40)
        updates += random.uniform(20, 60)
        path_changes = random.randint(4, 12)
    reach = max(0.0, min(100.0, reach))
    payload = {
        "thousandeyes.account.id": ACCOUNT_ID,
        "thousandeyes.account.name": ACCOUNT_GROUP_NAME,
        "thousandeyes.stream.id": STREAM_ID,
        "thousandeyes.test.id": test["testId"],
        "thousandeyes.test.name": test["testName"],
        "thousandeyes.test.type": "bgp",
        "thousandeyes.test.domain": test.get("domain", "cea"),
        "thousandeyes.monitor.location": monitor["location"],
        "thousandeyes.monitor.name": monitor["monitorName"],
        "thousandeyes.permalink": permalink(test["testId"]),
        "metric_name:bgp.reachability": round(reach, 2),
        "metric_name:bgp.updates.count": int(updates),
        "metric_name:bgp.path_changes.count": path_changes,
    }
    return _envelope(ST_METRIC, payload, ts=ts)


def emit_trace(ts: datetime) -> dict:
    test = random.choice(PAGELOAD_TESTS + TRANSACTION_TESTS + API_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    bl = BASELINES[test["testId"]]
    deg = maybe_degrade()
    duration_ms = jittered(bl["responseTime"])
    if deg == "minor":
        duration_ms *= random.uniform(1.5, 3.0)
    elif deg == "major":
        duration_ms *= random.uniform(4.0, 10.0)

    start_ns = int(ts.timestamp() * 1e9)
    end_ns = int(start_ns + duration_ms * 1e6)
    status_code = 0  # OTel: 0=Unset, 1=Ok, 2=Error
    if deg == "major":
        status_code = 2
    elif deg == "minor":
        status_code = random.choice([0, 2])

    payload = {
        **base_test_attrs(test, agent),
        "trace_id": hex_id(16),
        "span_id": hex_id(8),
        "parent_span_id": "",
        "span_name": f"{test['testType']} {test['target']}",
        "span_kind": "client",
        "start_time_unix_nano": start_ns,
        "end_time_unix_nano": end_ns,
        "status_code": status_code,
        "status_message": "" if status_code == 0 else "performance degradation",
        "server.address": test["target"],
        "server.port": test.get("port", 443),
        "http.request.method": "GET",
        "http.response.code": 200 if status_code != 2 else 503,
        "url.full": test["url"],
    }
    return _envelope(ST_TRACE, payload, ts=ts)


def emit_path_vis(ts: datetime) -> dict:
    test = random.choice(NETWORK_TESTS + HTTP_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    deg = maybe_degrade()
    n_hops = random.randint(4, 14)
    response_time = jittered(40 + n_hops * 4)
    if deg != "normal":
        response_time *= random.uniform(1.3, 3.0)

    hops = []
    for h in range(1, n_hops + 1):
        hops.append({
            "ttl": h,
            "ipAddress": f"10.{random.randint(0, 250)}.{random.randint(0, 250)}.1",
            "rtt": round(jittered(response_time / n_hops), 2),
            "asn": random.choice(["AS3257", "AS2914", "AS7018",
                                  "AS8075", "AS16509", "AS6453"]),
        })

    payload = {
        "agent": {"agentId": agent["agentId"]},
        "agentId": agent["agentId"],
        "sourceIp": f"10.0.{int(agent['agentId']) % 250}.10",
        "serverIp": test.get("target", ""),
        "server": test.get("target", ""),
        "pathTraces": [{
            "responseTime": round(response_time, 2),
            "numberOfHops": n_hops,
            "hops": hops,
        }],
        "originalTargetProfile": {"protocol": test.get("transport", "tcp")},
        "thousandeyes.test.id": test["testId"],
        "thousandeyes.test.name": test["testName"],
        "thousandeyes.test.type": test["testType"],
        "thousandeyes.test.domain": test.get("domain", "cea"),
        "thousandeyes.account.id": ACCOUNT_ID,
        "thousandeyes.account.name": ACCOUNT_GROUP_NAME,
        "startTime": int(ts.timestamp()),
        "endTime": int(ts.timestamp()) + 60,
    }
    return _envelope(ST_PATH_VIS, payload, ts=ts)


def emit_event(ts: datetime) -> dict:
    tmpl = random.choice(PLATFORM_EVENT_TEMPLATES)
    target_test = random.choice(NETWORK_TESTS + HTTP_TESTS)
    is_active = random.random() < 0.6
    payload = {
        "id": f"evt-{secrets.token_hex(6)}",
        "title": tmpl["title"],
        "type": tmpl["type"],
        "typeName": tmpl["typeName"],
        "severity": tmpl["severity"],
        "state": "active" if is_active else "cleared",
        "startDate": (ts - timedelta(minutes=random.randint(2, 30))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "endDate": None if is_active else ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aid": ACCOUNT_ID,
        "affectedTargets": {"total": random.randint(1, 50)},
        "test_description": target_test["testName"],
    }
    return _envelope(ST_EVENT, payload, ts=ts)


def emit_activity(ts: datetime) -> dict:
    user = random.choice(USERS)
    action = random.choice(ACTIVITY_EVENTS)
    payload = {
        "thousandeyes.account.id": ACCOUNT_ID,
        "thousandeyes.account.name": ACCOUNT_GROUP_NAME,
        "thousandeyes.user.id": user["id"],
        "thousandeyes.user.name": user["name"],
        "thousandeyes.user.email": user["email"],
        "thousandeyes.user.address": user["address"],
        "event": action,
        "thousandeyes.activitylog.resources":
            [f"test:{random.choice(NETWORK_TESTS + HTTP_TESTS)['testId']}"],
        "date": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return _envelope(ST_ACTIVITY, payload, ts=ts)


def emit_alert(ts: datetime) -> dict:
    rule = random.choice(ALERT_RULES)
    test = random.choice(NETWORK_TESTS + HTTP_TESTS + VOICE_TESTS)
    agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
    triggered_ms = int((ts - timedelta(minutes=random.randint(1, 30))).timestamp() * 1000)
    cleared_ms = int(ts.timestamp() * 1000) if random.random() < 0.4 else None

    severity_map = {"INFO": "1", "MINOR": "3", "MAJOR": "5", "CRITICAL": "6"}
    sev = rule["severity"]

    alert_id = f"alert-{secrets.token_hex(4)}"
    target_str = test.get("target") or test.get("url", "")

    # Match the TA's THOUSANDEYES_WEBHOOK_PAYLOAD_TEMPLATE exactly.
    payload = {
        "eventId": f"{rule['ruleId']}-{alert_id}",
        "eventType": "THOUSANDEYES_ALERT_NOTIFICATION",
        "id": rule["ruleId"],
        "type": "2",
        "accountId": ACCOUNT_ID,
        "orgId": ORG_ID,
        "testId": test["testId"],
        "thousandeyes_test_id": test["testId"],
        "test_description": test["testName"],
        "test_type": test["testType"],
        "itsiDrilldownURI": permalink(test["testId"]),
        "severity_id": severity_map[sev],
        "vendor_severity": sev,
        "app": "THOUSANDEYES",
        "src": target_str,
        "signature": rule["ruleName"],
        "alert_type": rule["alertType"],
        "alert": {
            "id": alert_id,
            "type": rule["alertType"],
            "severity": sev,
            "test": {"name": test["testName"]},
            "targets": [target_str],
            "rule": {
                "id": rule["ruleId"],
                "name": rule["ruleName"],
                "expression": rule["expression"],
                "notes": "",
            },
            "triggered": triggered_ms,
            "cleared": cleared_ms,
            "details": [{
                "metricsAtStart": rule["expression"].replace(">=", ":"),
                "source": {
                    "id": agent["agentId"],
                    "name": agent["agentName"],
                    "asn": "AS8075",
                },
            }],
        },
    }
    if cleared_ms is None:
        del payload["alert"]["cleared"]
    return _envelope(ST_ALERTS, payload, ts=ts,
                     host=agent["agentName"], source=TE_SOURCE_WEBHOOK)


# ---------------------------------------------------------------------------
# Stream registry
# ---------------------------------------------------------------------------
METRIC_EMITTERS = [
    (emit_metric_network, 0.30),
    (emit_metric_http, 0.22),
    (emit_metric_pageload, 0.12),
    (emit_metric_transaction, 0.08),
    (emit_metric_api, 0.10),
    (emit_metric_dns, 0.06),
    (emit_metric_voice, 0.06),
    (emit_metric_bgp, 0.06),
]


def emit_metric_any(ts: datetime) -> dict:
    """Pick a metric sub-emitter weighted by realistic test mix."""
    r = random.random()
    cum = 0.0
    for fn, w in METRIC_EMITTERS:
        cum += w
        if r < cum:
            return fn(ts)
    return METRIC_EMITTERS[-1][0](ts)


STREAM_EMITTERS = {
    "metric": emit_metric_any,
    "trace": emit_trace,
    "path-vis": emit_path_vis,
    "event": emit_event,
    "activity": emit_activity,
    "alerts": emit_alert,
}


# ---------------------------------------------------------------------------
# Legacy mode (pre-TA event format) — kept reachable via --legacy
# ---------------------------------------------------------------------------
def emit_legacy(ts: datetime) -> dict:
    """Backwards-compatible emitter — sourcetypes thousandeyes:network/http/alert."""
    r = random.random()
    if r < 0.50:
        test = random.choice(NETWORK_TESTS)
        agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
        bl = BASELINES[test["testId"]]
        deg = maybe_degrade()
        lat = jittered(bl["latency"])
        jit = jittered(bl["jitter"])
        loss = 0.0
        if deg == "minor":
            lat *= random.uniform(1.5, 2.5)
            jit *= random.uniform(1.5, 3.0)
            loss = random.uniform(0.1, 1.0)
        elif deg == "major":
            lat *= random.uniform(3.0, 8.0)
            jit *= random.uniform(3.0, 6.0)
            loss = random.uniform(2.0, 15.0)
        ev = {
            "type": "agent-to-server",
            "testId": test["testId"], "testName": test["testName"],
            "server": f"{test['target']}:443",
            "protocol": test.get("transport", "tcp").upper(),
            "date": ts.isoformat(),
            "agent": {
                "agentId": agent["agentId"], "agentName": agent["agentName"],
                "agentType": "enterprise", "location": agent["location"],
                "countryId": agent["countryId"],
                "coordinates": {"latitude": agent["lat"], "longitude": agent["lon"]},
            },
            "avgLatency": round(lat, 2), "jitter": round(jit, 2),
            "loss": round(loss, 2),
        }
        return {
            "time": ts.timestamp(), "host": "thousandeyes.cloud",
            "source": "thousandeyes:api",
            "sourcetype": "thousandeyes:network", "event": ev,
        }
    elif r < 0.85:
        test = random.choice(HTTP_TESTS)
        agent = random.choice([a for a in AGENTS if a["agentType"] != "endpoint"])
        bl = BASELINES[test["testId"]]
        deg = maybe_degrade()
        rt = jittered(bl["responseTime"])
        status = 200
        avail = 100.0
        if deg == "minor":
            rt *= random.uniform(2, 4)
            if random.random() < 0.3:
                status = random.choice([502, 503, 504])
                avail = random.uniform(60, 95)
        elif deg == "major":
            rt *= random.uniform(5, 15)
            status = random.choice([500, 502, 503, 504])
            avail = random.uniform(20, 60)
        ev = {
            "type": "http-server",
            "testId": test["testId"], "testName": test["testName"],
            "url": test["url"], "date": ts.isoformat(),
            "responseTime": round(rt, 1), "responseCode": status,
            "availability": round(avail, 1),
            "agent": {
                "agentId": agent["agentId"], "agentName": agent["agentName"],
                "agentType": "enterprise", "location": agent["location"],
                "coordinates": {"latitude": agent["lat"], "longitude": agent["lon"]},
            },
        }
        return {
            "time": ts.timestamp(), "host": "thousandeyes.cloud",
            "source": "thousandeyes:api",
            "sourcetype": "thousandeyes:http", "event": ev,
        }
    else:
        # Wrap the new alert format under the legacy sourcetype name.
        env = emit_alert(ts)
        env["sourcetype"] = "thousandeyes:alert"
        env["source"] = "thousandeyes:api"
        env["host"] = "thousandeyes.cloud"
        return env


# ---------------------------------------------------------------------------
# HEC delivery
# ---------------------------------------------------------------------------
def _ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _is_transient_hec_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        if isinstance(r, OSError) and r.errno in (
                errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH):
            return True
        if isinstance(r, ConnectionResetError):
            return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def wait_for_hec(splunk_url: str, token: str, verify_ssl: bool,
                 interval: float = 5.0, max_wait: float = 600.0) -> None:
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
            print(f"HEC ready at {base} (after {attempt} attempt(s))",
                  file=sys.stderr)
            return
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504, 404):
                if attempt == 1 or attempt % 6 == 0:
                    print(f"Waiting for HEC at {base}… (HTTP {e.code})",
                          file=sys.stderr)
                time.sleep(interval)
            else:
                print(f"HEC health check failed: HTTP {e.code} {e.reason!r}",
                      file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            if attempt == 1 or attempt % 6 == 0:
                print(f"Waiting for HEC at {base}… ({e!r})", file=sys.stderr)
            time.sleep(interval)
    print(f"Timeout: HEC did not become ready within {max_wait}s at {base}",
          file=sys.stderr)
    sys.exit(1)


def send_event(envelope: dict, splunk_url: str, token: str,
               index: str, verify_ssl: bool = False,
               retries: int = 5, retry_delay: float = 3.0) -> None:
    payload = dict(envelope)
    payload["index"] = index
    body = json.dumps(payload).encode("utf-8")

    url = f"{splunk_url.rstrip('/')}/services/collector/event"
    ctx = _ssl_context(verify_ssl)
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Splunk {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                resp.read()
                return
        except Exception as e:
            if _is_transient_hec_error(e) and attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise


# ---------------------------------------------------------------------------
# Stream picking
# ---------------------------------------------------------------------------
def pick_stream(enabled: list[str]) -> str:
    weights = [DEFAULT_MIX[s] for s in enabled]
    total = sum(weights) or 1.0
    weights = [w / total for w in weights]
    r = random.random()
    cum = 0.0
    for s, w in zip(enabled, weights):
        cum += w
        if r < cum:
            return s
    return enabled[-1]


def generate_one(enabled_streams: list[str], ts: datetime,
                 legacy: bool) -> dict:
    if legacy:
        return emit_legacy(ts)
    return STREAM_EMITTERS[pick_stream(enabled_streams)](ts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ThousandEyes synthetic data generator (TA-compatible).")
    parser.add_argument("--splunk-url", default="https://localhost:8088",
                        help="Splunk HEC URL")
    parser.add_argument("--token", default=None, help="Splunk HEC token")
    parser.add_argument("--index", default="thousandeyes",
                        help="Target Splunk index")
    parser.add_argument("--interval", type=float, default=10,
                        help="Seconds between events in continuous mode")
    parser.add_argument("--burst", type=int, default=0,
                        help="Send N events and exit (0 = continuous)")
    parser.add_argument("--backfill", type=float, default=0,
                        help="Hours of backfill to send before going live "
                             "(useful so dashboards have data immediately)")
    parser.add_argument("--backfill-rate", type=int, default=120,
                        help="Approx. events per simulated hour during "
                             "backfill (default: 120 = ~1 every 30s)")
    parser.add_argument("--streams", default="all",
                        help="Comma-separated streams to emit. Choices: "
                             "metric,trace,path-vis,event,activity,alerts,all")
    parser.add_argument("--legacy", action="store_true",
                        help="Emit pre-TA legacy event format")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print events to stdout instead of sending")
    parser.add_argument("--verify-ssl", action="store_true",
                        help="Verify SSL certificates")
    parser.add_argument("--skip-hec-wait", action="store_true",
                        help="Do not wait for HEC /health before sending")
    parser.add_argument("--hec-wait-max", type=float, default=600.0,
                        help="Seconds to wait for HEC to accept connections")
    args = parser.parse_args()

    # Resolve enabled streams.
    if args.streams.strip().lower() == "all":
        enabled = list(STREAM_EMITTERS.keys())
    else:
        enabled = [s.strip() for s in args.streams.split(",") if s.strip()]
        invalid = [s for s in enabled if s not in STREAM_EMITTERS]
        if invalid:
            print(f"Error: unknown streams: {invalid}. "
                  f"Valid: {list(STREAM_EMITTERS)}", file=sys.stderr)
            sys.exit(1)

    if not args.dry_run and not args.token:
        print("Error: --token required (or use --dry-run)", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not args.skip_hec_wait:
        wait_for_hec(args.splunk_url, args.token, args.verify_ssl,
                     interval=5.0, max_wait=float(args.hec_wait_max))

    mode = "legacy" if args.legacy else "TA-compatible (cisco:thousandeyes:*)"
    print(f"Mode: {mode}; streams: {enabled if not args.legacy else 'legacy'}",
          file=sys.stderr)

    # Optional backfill.
    if args.backfill > 0 and not args.legacy:
        n_backfill = int(args.backfill * args.backfill_rate)
        print(f"Backfilling {n_backfill} events over the last "
              f"{args.backfill}h…", file=sys.stderr)
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=args.backfill)
        for i in range(n_backfill):
            # Spread events linearly across the backfill window.
            ts = start + timedelta(
                seconds=(i / max(n_backfill - 1, 1))
                * args.backfill * 3600.0)
            env = generate_one(enabled, ts, legacy=False)
            if args.dry_run:
                print(json.dumps(env))
            else:
                send_event(env, args.splunk_url, args.token,
                           args.index, args.verify_ssl)
            if (i + 1) % 200 == 0:
                print(f"  backfill {i + 1}/{n_backfill}", file=sys.stderr)
        print(f"Backfill done ({n_backfill} events).", file=sys.stderr)

    # Burst mode → exit when done.
    if args.burst > 0:
        sent = 0
        now = datetime.now(timezone.utc)
        for i in range(args.burst):
            env = generate_one(enabled, now, legacy=args.legacy)
            if args.dry_run:
                print(json.dumps(env, indent=2))
            else:
                send_event(env, args.splunk_url, args.token,
                           args.index, args.verify_ssl)
            sent += 1
            if sent % 50 == 0:
                print(f"  sent {sent}/{args.burst}", file=sys.stderr)
        print(f"Done: {sent} events sent to {args.index}", file=sys.stderr)
        return

    # Continuous mode.
    print(f"Continuous mode: sending events every {args.interval}s "
          f"to index={args.index}", file=sys.stderr)
    print("Press Ctrl+C to stop", file=sys.stderr)
    count = 0
    try:
        while True:
            now = datetime.now(timezone.utc)
            n = random.randint(1, 4)
            for _ in range(n):
                env = generate_one(enabled, now, legacy=args.legacy)
                if args.dry_run:
                    print(json.dumps(env))
                else:
                    send_event(env, args.splunk_url, args.token,
                               args.index, args.verify_ssl)
                count += 1
            print(f"  [{now.strftime('%H:%M:%S')}] sent {n} events "
                  f"(total: {count})", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped. Total events sent: {count}", file=sys.stderr)


if __name__ == "__main__":
    main()
