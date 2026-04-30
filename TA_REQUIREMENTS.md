# TA-kompatibilitets-krav — Cisco ThousandEyes App for Splunk

> Reverse-engineered fra `ta_cisco_thousandeyes` v0.6.0 installert i `splunk-mac`
> Kilde: `~/dev/thousandeyes/_ta_inspection/`
> Mål: vår generator skal produsere events som er 100% lesbare av appens
> innebygde props.conf, datamodell og dashboards.

## TL;DR

Hele integrasjonen sender til HEC `/services/collector/event` (events-index,
ikke metrics-index). Datamodellen `Cisco_ThousandEyes` er det dashboardene
spør mot via `tstats summariesonly`, og field-aliases i `props.conf` mapper
fra OpenTelemetry-attributter (`thousandeyes.test.id`, `server.address`,
osv.) til de leste feltene. Numeriske målinger sendes som event-felt med
key-format `metric_name:<otel.metric.name>` (med kolon i nøkkelen) — dette
er Splunks "metrics-as-events" mønster.

---

## 1. Source / sourcetypes

| Konsept | Verdi |
|---|---|
| `source` (alle streams) | `cisco:thousandeyes:stream` |
| `source` (alerts webhook) | `cisco:thousandeyes:webhook` |

### Sourcetypes (6 stk)

| Sourcetype | Brukes for | Levering |
|---|---|---|
| `cisco:thousandeyes:metric` | Tests Stream — Metrics (alle test-typer) | OTel push til HEC |
| `cisco:thousandeyes:trace` | Tests Stream — Traces (page-load, web-transactions, api) | OTel push til HEC |
| `cisco:thousandeyes:path-vis` | Path Visualization | API polling fra TA |
| `cisco:thousandeyes:event` | ThousandEyes platform events | API polling fra TA |
| `cisco:thousandeyes:activity` | User activity audit log | OTel push til HEC |
| `cisco:thousandeyes:alerts` | Alert notifications | Webhook push til HEC |
| `cisco:thousandeyes:account-group` | Internal lookup-population | (for our use: skip eller send sjelden) |

### HEC-endepunkter

Alt går til `/services/collector/event`. **Ingen** av sourcetypene bruker
metrics-indexer eller `/services/collector` (uten `/event`-suffiks).

## 2. Index-konvensjon

TA-en bruker 6 makroer (alle `index=*` som default — kan overstyres) som
peker på events-indexer:

| Makro | Sourcetype | Vår foreslåtte index |
|---|---|---|
| `stream_index` | `cisco:thousandeyes:metric` | `te_stream` |
| `path_viz_index` | `cisco:thousandeyes:path-vis` | `te_path_viz` |
| `event_index` | `cisco:thousandeyes:event` | `te_event` |
| `activity_index` | `cisco:thousandeyes:activity` | `te_activity` |
| `trace_index` | `cisco:thousandeyes:trace` | `te_trace` |
| `alert_index` | `cisco:thousandeyes:alerts` | `te_alert` |

For mac-prototyping kan alle dele én `thousandeyes`-index (slik vi har i
dag) — TA-makroene aksepterer `index=*`. For prod bør vi splitte.

## 3. Test-typer (`thousandeyes.test.type`)

| Kategori | Verdier |
|---|---|
| Network (eventtype `cisco_thousandeyes_test_metrics_network`) | `agent-to-server`, `agent-to-agent`, `bgp`, `dns-server`, `dns-trace`, `dns-dnessec`, `dns-sec` |
| Web/App (eventtype `cisco_thousandeyes_test_metrics_web`) | `http-server`, `page-load`, `api`, `web-transaction`, `ftp-server`, `sip`, `rtp` |
| (Trace-only typer) | `page-load`, `web-transactions`, `api` |
| (Endpoint-only typer) | `agent-to-server`, `http-server` |

> NB: i `eventtypes.conf` er `web-transaction` (entall) — i `test_types`-konstantene heter den `web-transactions` (flertall). Vi følger TA-koden som sjekker mot `web-transactions` for traces.

## 4. Metric_name namespace (43 målinger)

Alle metrikker sendes som event-felt der nøkkelen er `metric_name:<otel.name>`
og verdien er det numeriske målet. Alle key-navn fra datamodellen
`Cisco_ThousandEyes.Test_Metrics`:

### Network
- `metric_name:network.latency` (ms)
- `metric_name:network.loss` (% — 0–100)
- `metric_name:network.jitter` (ms)

### BGP
- `metric_name:bgp.path_changes.count`
- `metric_name:bgp.reachability` (% — 0–100)
- `metric_name:bgp.updates.count`

### DNS
- `metric_name:dns.lookup.availability`
- `metric_name:dns.lookup.duration`
- `metric_name:dns.lookup.validity`

### HTTP
- `metric_name:http.client.request.duration` (sek — multipliseres ×1000 til ms i TA)
- `metric_name:http.server.request.availability` (%)
- `metric_name:http.server.throughput` (B/s)

### FTP
- `metric_name:ftp.client.request.duration`
- `metric_name:ftp.server.request.availability`
- `metric_name:ftp.server.throughput`

### SIP
- `metric_name:sip.client.request.duration`
- `metric_name:sip.client.request.total_time`
- `metric_name:sip.server.request.availability`

### RTP (Voice)
- `metric_name:rtp.client.request.mos` (1–5)

### Web (page-load + api + web-transactions)
- `metric_name:web.page_load.completion`
- `metric_name:web.page_load.duration`
- `metric_name:web.transaction.completion`
- `metric_name:web.transaction.duration`
- `metric_name:api.step.completion`
- `metric_name:api.step.duration`

## 5. Påkrevde attributter på `cisco:thousandeyes:metric`-events

OTel-semantic-conventions + TE-spesifikke attributter:

| Felt | Type | Eksempel | Bruk i datamodell |
|---|---|---|---|
| `thousandeyes.account.id` | string | `"990001"` | required for korrelasjon |
| `thousandeyes.test.id` | string | `"50001"` | unik test |
| `thousandeyes.test.name` | string | `"WAN - Oslo to Stavanger"` | |
| `thousandeyes.test.type` | string | `"agent-to-server"` | filter (se §3) |
| `thousandeyes.test.domain` | string | `"cea"` (Cloud & Enterprise) eller `"endpoint"` | |
| `thousandeyes.source.agent.id` | string | `"10001"` | dvc i datamodell |
| `thousandeyes.source.agent.name` | string | `"te-agent-oslo-01"` | |
| `thousandeyes.source.agent.location` | string | `"Oslo, Norway"` | |
| `thousandeyes.target.agent.id` | string | (kun for agent-to-agent) | dvc for metric |
| `thousandeyes.target.agent.name` | string | (kun for agent-to-agent) | |
| `thousandeyes.permalink` | string | URL til testen i TE UI | |
| `thousandeyes.stream.id` | string | f.eks. `"stream-abc123"` | |
| `thousandeyes.monitor.location` | string | (BGP-monitor) | |
| `thousandeyes.monitor.name` | string | (BGP-monitor) | |
| `server.address` | string | `"10.20.1.1"` eller `"sap.internal.corp"` | dest |
| `server.port` | number | `443` | |
| `network.transport` | string | `"tcp"`, `"udp"`, `"icmp"` | |
| `network.io.direction` | string | (sjelden brukt) | |
| `dns.question.name` | string | (kun DNS-tester) | |
| `error.type` | string | (om feil) | error_code |
| `http.request.method` | string | `"GET"` | |
| `http.response.status_code` | number | `200` | |

## 6. Påkrevde attributter på `cisco:thousandeyes:trace`-events

OTLP span-format med flat JSON:

| Felt | Type | Eksempel |
|---|---|---|
| `trace_id` | string (32 hex) | `"a1b2c3..."` |
| `span_id` | string (16 hex) | `"d4e5f6..."` |
| `parent_span_id` | string (16 hex eller "") | |
| `span_name` | string | `"GET /portal"` |
| `span_kind` | string | `"client"` eller `"server"` |
| `start_time_unix_nano` | number | `1714492800000000000` |
| `end_time_unix_nano` | number | `1714492800412000000` |
| `status_code` | number | `0`=Unset, `1`=Ok, `2`=Error |
| `status_message` | string | |
| `thousandeyes.account.id`, `.test.id`, `.test.name`, `.test.type`, `.source.agent.id`, `.source.agent.name`, `.source.agent.location`, `.permalink`, `.stream.id` | (samme som metric) | |
| `server.address`, `server.port`, `http.request.method`, `http.response.code`, `url.full` | OTel HTTP attrs | |

`duration` og `response_time` er EVAL i props.conf — vi trenger ikke sende dem.

## 7. `cisco:thousandeyes:path-vis`-events (path visualization)

Avviker fra OTel-konvensjonen — bruker TE API v7 native struktur:

```json
{
  "agent": {"agentId": "10001"},
  "agentId": "10001",
  "sourceIp": "10.0.5.15",
  "serverIp": "10.20.1.1",
  "server": "10.20.1.1",
  "pathTraces": [{"responseTime": 12.4, "numberOfHops": 7, "hops": [...]}],
  "networkProfile": {"wirelessProfile": {"channel": 36, "ssid": "...", "phyMode": "..."}},
  "originalTargetProfile": {"protocol": "icmp"}
}
```

Hop-listen i `pathTraces[].hops[]` har ikke spesifikk struktur i TA-en sin
props.conf, men feltet `hops` (etter alias `pathTraces{}.numberOfHops`)
brukes i datamodellen `Path_Viz`.

## 8. `cisco:thousandeyes:event`-events (TE platform events)

Også API v7 native:

```json
{
  "id": "evt-12345",
  "title": "Network outage detected",
  "type": "outage",
  "typeName": "Outage",
  "severity": "high",
  "state": "active",
  "startDate": "2026-04-30T13:42:00Z",
  "endDate": null,
  "aid": "990001",
  "affectedTargets": {"total": 47}
}
```

## 9. `cisco:thousandeyes:activity`-events (user audit)

OTel log signal med `thousandeyes.user.*` namespace:

```json
{
  "thousandeyes.account.id": "990001",
  "thousandeyes.account.name": "Production",
  "thousandeyes.user.id": "user-123",
  "thousandeyes.user.name": "alice@corp.com",
  "thousandeyes.user.email": "alice@corp.com",
  "thousandeyes.user.address": "10.0.0.5",
  "event": "test.created",
  "thousandeyes.activitylog.resources": ["test:50001"],
  "date": "2026-04-30T13:42:00Z"
}
```

(Kan også brukes legacy-felter `aid`, `user`, `event`, `date` direkte —
EVALs i props.conf coalescer.)

## 10. `cisco:thousandeyes:alerts`-events (webhook)

**Mest detaljerte format**, definert i TA-en sin `THOUSANDEYES_WEBHOOK_PAYLOAD_TEMPLATE`
(Handlebars). Vi må produsere det rendrede resultatet:

```json
{
  "eventId": "<rule.id>-<alert.id>",
  "eventType": "THOUSANDEYES_ALERT_NOTIFICATION",
  "id": "<rule.id>",
  "type": "<type.id>",
  "accountId": "<rule.account.id>",
  "orgId": "<rule.account.organization.id>",
  "testId": "<test.id>",
  "thousandeyes_test_id": "<test.id>",
  "test_description": "<test.description>",
  "test_type": "<test.testType>",
  "itsiDrilldownURI": "https://app.thousandeyes.com/network-app-synthetics/views/?__a=<accountId>&testId=<testId>",
  "severity_id": "1|3|5|6 mapping fra INFO|MINOR|MAJOR|CRITICAL",
  "vendor_severity": "<INFO|MINOR|MAJOR|CRITICAL>",
  "app": "THOUSANDEYES",
  "src": "<first target description>",
  "signature": "<rule.name>",
  "alert_type": "<rule.alertType.id>",
  "alert": {
    "id": "<alert.id>",
    "type": "<rule.alertType.id>",
    "severity": "<INFO|MINOR|MAJOR|CRITICAL>",
    "test": {"name": "<test.name>"},
    "targets": ["<target1.description>", ...],
    "rule": {
      "id": "<rule.id>",
      "name": "<rule.name>",
      "expression": "<formatted>",
      "notes": "<rule.notes>"
    },
    "triggered": <epochMillis>,
    "cleared": <epochMillis>|absent,
    "details": [
      {
        "metricsAtStart": "...",
        "metricsAtEnd": "..."|absent,
        "source": {"id": "<agent.id>", "name": "<agent.name>", "asn": "<asn.name>"|absent}
      }, ...
    ]
  }
}
```

Severity_id-mapping:
- `INFO` → `1`
- `MINOR` → `3`
- `MAJOR` → `5`
- `CRITICAL` → `6`

## 11. HEC-event wrapper (alle sourcetypes)

```json
{
  "time": <unix_seconds, kan ha desimaler>,
  "host": "<agent name eller thousandeyes-otel>",
  "source": "cisco:thousandeyes:stream",
  "sourcetype": "cisco:thousandeyes:metric",
  "index": "<routes via HEC token default>",
  "event": { ... payload ... }
}
```

## 12. Forskjeller fra dagens generator (gap-liste)

| Aspekt | Dagens generator | TA krever |
|---|---|---|
| `source` | `thousandeyes:api` | `cisco:thousandeyes:stream` (eller `:webhook` for alerts) |
| `sourcetype` | `thousandeyes:network` / `:http` / `:alert` | `cisco:thousandeyes:metric` / `:trace` / `:alerts` (+ 3 til) |
| `host` | `thousandeyes.cloud` | f.eks. `thousandeyesotel` eller agent-name |
| Numeriske felt | Topp-nivå (`avgLatency: 12.5`) | `metric_name:network.latency: 12.5` (med colon i key) |
| Felt-navngivning | `agent.agentName`, `agent.location` | `thousandeyes.source.agent.name`, `thousandeyes.source.agent.location` |
| Test-type | implisitt via sourcetype | eksplisitt `thousandeyes.test.type` |
| Path-visualization | Bare `numberOfHops` | Egen sourcetype med `pathTraces[].hops[]` struktur |
| Alerts | Vår egen API v7-lookalike | Webhook-template-rendret format med eksakte feltnavn |
| Datatyper vi ikke har | — | Traces (page-load/transaction/api), Activity logs, Platform events, BGP-metrikker, DNS, FTP, SIP, RTP/MOS |

## 13. Levering — hvilke streams trenger faktisk implementeres

| Prioritet | Stream | Hvorfor |
|---|---|---|
| Må | `metric` | Driver alle dashboard-KPI-er for network + http |
| Må | `alerts` | Driver alert-dashboardet |
| Bør | `path-vis` | Driver path-visualization-paneler i Network-dashboardet |
| Bør | `event` | Driver platform Events-dashboardet |
| Kan | `trace` | Bare for page-load/transaction/api-tester (vi har ikke disse i dag) |
| Kan | `activity` | User-audit, mest interessant for SOC-historie |

For 100% kompatibilitet bør vi dekke alle 6 — men vi kan levere i faser. Anbefalt rekkefølge: metric → alerts → path-vis → event → activity → trace.

## 14. Validering — hvordan vi vet vi har det riktig

1. Send burst med våre nye events.
2. Splunk-søk:
   ```spl
   index=* sourcetype="cisco:thousandeyes:metric" | head 5
   ```
   Verifiser at `thousandeyes.*` og `metric_name:*` kommer ut riktig.
3. Bygg datamodell-acceleration:
   ```
   Settings > Data Models > Cisco ThousandEyes > Edit Acceleration > Accelerate
   ```
4. Åpne `Cisco ThousandEyes App for Splunk > Network`-dashboardet — KPI-er
   må fylles ut.
5. Åpne `Application` (HTTP), `Voice`, `Alerts` — samme.

## Kilder

| Fil | Hva vi lærte |
|---|---|
| `default/props.conf` | 6 sourcetypes + alle FIELDALIAS-er + EVAL-er |
| `default/eventtypes.conf` | Test-type-grupperingene (web vs network) |
| `default/macros.conf` | 6 index-makroer |
| `default/datamodels.conf` + `data/models/Cisco_ThousandEyes.json` | Komplett feltliste pr objekt |
| `default/inputs.conf` | Modular input-typer + default-sourcetypes |
| `bin/thousandeyes_constant.py` | Source/sourcetype-konstantene + webhook-template + test-type-listen |
| `data/ui/views/network.xml` + `application.xml` | Bevis på `Test_Metrics.metric_name:network.latency`-bruk i tstats |
