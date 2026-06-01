# S3DF Status Adapter - Technical Design

## Summary

`app/s3df/status_adapter.py` implements the IRI `/status` router for S3DF. IRI owns status collection at runtime: it periodically runs configured health checks, evaluates status-pusher-style conditions, and caches the latest status for each resource in memory.

The external status repositories are references only:

- `status/s3df-status` defines the user-facing resource vocabulary IRI should expose.
- `status/status-pusher` demonstrates the check/evaluate model IRI now runs internally.
- `s3df-status-logs` is not read by IRI.

IRI must not fetch GitHub-hosted report logs or depend on `status-pusher`/`s3df-status` at runtime.

## Canonical resources

`/status/resources` returns exactly the eleven S3DF resources from `status/s3df-status/public/urls.cfg`:

| IRI id | Name | Resource type | Group |
| --- | --- | --- | --- |
| `s3df-ssh-bastions` | `SSH Bastions` | `service` | `access` |
| `s3df-interactive-nodes` | `Interactive Nodes` | `compute` | `compute` |
| `s3df-docs` | `S3DF Docs` | `website` | `documentation` |
| `s3df-batch-servers` | `Batch Servers` | `compute` | `compute` |
| `s3df-slurm` | `Slurm` | `service` | `compute` |
| `s3df-monitoring` | `Monitoring` | `service` | `operations` |
| `s3df-coact` | `Coact` | `service` | `accounts` |
| `s3df-ondemand` | `OnDemand` | `website` | `access` |
| `s3df-kubernetes` | `Kubernetes` | `system` | `platform` |
| `s3df-storage` | `Storage` | `storage` | `storage` |
| `s3df-dtns` | `DTNs` | `network` | `data-transfer` |

The old direct-check resources (`s3df-ssh-gateway`, `s3df-slurmctld`, `s3df-slurmdbd`) are not exposed as separate resources. They are implementation details of higher-level resource checks.

## Runtime workflow

```text
Prometheus / InfluxDB / HTTP endpoints
        |
        v
IRI HealthChecker
  query live source
  evaluate condition
  aggregate checks per resource
        |
        v
StatusPoller
  repeats every S3DF_STATUS_POLL_INTERVAL
        |
        v
StatusStore cache
  Resource.current_status
  transition Events
  auto Incidents
        |
        v
GET /status/resources
GET /status/events
GET /status/incidents
```

The first `/status/...` request starts the poller lazily and awaits one initial poll. Later requests return cached statuses; they do not query monitoring backends per request.

## Health checks

Each resource has zero or more checks. A check produces a scalar value and an evaluation rule:

```text
observed value + up condition + optional degraded condition -> Status
```

Supported backends:

| Backend | Signal | Default `up_when` for configured checks |
| --- | --- | --- |
| `prometheus` | Instant query scalar from `/api/v1/query` | `eq 1` |
| `influxdb` | Last scalar from `/query` result series | `eq 1` |
| `http` | HTTP response status code | `eq 200` |

Built-in checks:

- `SSH Bastions`: Prometheus query `avg( avg_over_time(nmap_port_state{service=\`ssh\`,group=\`s3df\`}[5m]) )`, `eq 1`.
- `Slurm`: aggregate InfluxDB `monit_process` checks for `slurmctld` and `slurmdbd`, each `eq 1`.

Other resources can receive live status through IRI configuration.

## Check aggregation

Multiple checks for one resource are aggregated into one `Resource.current_status`:

- no checks -> `unknown`
- all checks `unknown` -> `unknown`
- any known check `down` -> `down`
- otherwise, any known check `degraded` -> `degraded`
- otherwise, all known checks `up` -> `up`

Unknown checks are ignored when at least one check produced a usable signal. This prevents a transient probe failure from creating a false outage if another check confirms the resource is healthy.

## Configuring additional live checks

Use `S3DF_STATUS_CHECKS_JSON` to attach IRI-owned checks to any canonical resource id. Configured checks are appended to built-in checks.

Example:

```json
{
  "s3df-docs": [
    {
      "backend": "http",
      "name": "docs-home",
      "url": "https://example.internal/docs/health",
      "up_when": {"comparator": "eq", "value": 200}
    }
  ],
  "s3df-kubernetes": [
    {
      "backend": "prometheus",
      "name": "kubernetes-api-ready",
      "query": "max(up{job=\"kubernetes-apiservers\"})",
      "up_when": {"comparator": "gte", "value": 1}
    }
  ],
  "s3df-storage": [
    {
      "backend": "influxdb",
      "name": "storage-check",
      "db_name": "telegraf",
      "query": "SELECT mean(\"status_code\") FROM \"storage_health\" WHERE time > now()-5m",
      "up_when": {"comparator": "eq", "value": 1}
    }
  ]
}
```

Invalid configuration raises an explicit startup/configuration error rather than silently dropping a check. Resources with no built-in or configured checks remain visible with `current_status=unknown`.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `S3DF_STATUS_CHECKS_JSON` | JSON mapping resource ids to extra health checks | `{}` |
| `S3DF_STATUS_POLL_INTERVAL` | Seconds between poll cycles | `60` |
| `S3DF_STATUS_HTTP_TIMEOUT` | Per-request timeout in seconds | `15` |
| `S3DF_SITE_ID` | Site id stamped onto returned resources | `s3df` |
| `S3DF_STATUS_TLS_VERIFY` | `true`, `false`, or CA bundle path for HTTP clients | `false` |
| `S3DF_PROMETHEUS_URL` | Prometheus endpoint | `https://prometheus.slac.stanford.edu` |
| `S3DF_INFLUXDB_URL` | InfluxDB endpoint | `https://influxdb.slac.stanford.edu` |
| `S3DF_INFLUXDB_DB` | Default InfluxDB database | `telegraf` |

`make dev-s3df` and Docker select `app.s3df.status_adapter.S3DFStatusAdapter` via `IRI_API_ADAPTER_status`.

## Events and incidents

The store is transition-driven:

- First observation emits a baseline event.
- A status change emits an event.
- `down` or `degraded` opens an unplanned incident if one is not already open.
- `down` <-> `degraded` updates the open incident.
- `up` closes the open incident with `resolution=completed`.
- `unknown` records an event but does not open or close incidents, because missing monitoring data is not a confirmed resource outage or recovery.

State is in-memory and per-process. Events and incidents are lost on restart and can diverge across multiple uvicorn workers.

## Known trade-offs and follow-ups

- Checks for resources beyond SSH and Slurm require IRI configuration until canonical Prometheus/InfluxDB/HTTP probes are supplied.
- There is no persistence for status history, events, or incidents.
- There is no planned-maintenance ingestion yet; all generated incidents are `unplanned`.
- The event log is append-only in memory and has no retention policy.
- Direct Prometheus/InfluxDB checks reduce responses to one scalar and do not implement multi-series aggregation beyond resource-level check aggregation.
- `S3DF_SITE_ID` defaults to `s3df`, while the facility adapter may still use a different site identifier until a stable cross-router site id is wired through.
