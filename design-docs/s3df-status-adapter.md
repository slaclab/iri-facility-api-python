# S3DF Status Adapter — Technical Design Report

## 1. Summary

This document describes the implementation plan for `app/s3df/status_adapter.py` — the
S3DF-specific adapter that fulfills the IRI status API contract. Today the status router
(`/status`) has no S3DF implementation; requests require `IRI_API_ADAPTER_status` be set
to the demo adapter. The goal is a production adapter that queries real S3DF monitoring
infrastructure (Prometheus, InfluxDB) and maps results into the IRI status model
(Resources, Events, Incidents).

The `status-pusher` CLI (separate repo) already queries these same backends and evaluates
health criteria — its query and evaluation logic serves as a reference for the data-plane
portion of this adapter.

---

## 2. Architecture Context

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                        IRI Facility API (FastAPI)                        │
 │                                                                         │
 │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  │
 │  │ /facility    │  │ /account     │  │ /compute     │  │ /status    │  │
 │  │ adapter ✓    │  │ adapter ✓    │  │ adapter ✓    │  │ adapter ✗  │  │
 │  └──────────────┘  └──────────────┘  └──────────────┘  └─────┬──────┘  │
 │                                                               │         │
 └───────────────────────────────────────────────────────────────┼─────────┘
                                                                 │
                            ┌────────────────────────────────────┘
                            │  implements
                            ▼
               ┌────────────────────────┐
               │  S3DFStatusAdapter     │
               │  app/s3df/status_      │
               │       adapter.py       │
               └───────────┬────────────┘
                           │
              ┌────────────┼─────────────────┐
              │            │                 │
              ▼            ▼                 ▼
     ┌──────────────┐ ┌────────────┐ ┌────────────────┐
     │ Prometheus   │ │ InfluxDB   │ │ Static config  │
     │ (live health)│ │ (telegraf) │ │ (resource defs)│
     └──────────────┘ └────────────┘ └────────────────┘
```

The existing `status-pusher` operates *outside* IRI as a scheduled cron/k8s job that
writes results to a git repo consumed by the Fettle dashboard. This adapter *replaces*
that indirection for the IRI API by querying backends directly at request time (or via a
background polling cache).

---

## 3. IRI Status API Contract

The abstract `FacilityAdapter` (in `app/routers/status/facility_adapter.py`) requires:

| Method           | Returns                  | Purpose                           |
|------------------|--------------------------|-----------------------------------|
| `get_resources`  | `list[Resource]`         | Filterable list of tracked resources |
| `get_resource`   | `Resource`               | Single resource by id             |
| `get_events`     | `list[Event]`            | Status-change events              |
| `get_event`      | `Event`                  | Single event by id                |
| `get_incidents`  | `list[Incident]`         | Planned/unplanned incidents       |
| `get_incident`   | `Incident`               | Single incident by id             |

### Key Models

```
Resource:
  id, name, description, last_modified, site_id, group, resource_type,
  current_status (up|down|degraded|unknown), capability_ids

Event:
  id, name, description, last_modified, occurred_at, status, resource_id,
  incident_id (optional)

Incident:
  id, name, description, last_modified, status, start, end, type
  (planned|unplanned|reservation), resolution
  (unresolved|cancelled|completed|extended|pending), resource_ids, event_ids
```

---

## 4. Reference: status-pusher

`status-pusher` is a Click CLI that:

1. Queries a metrics backend (Prometheus or InfluxDB).
2. Applies a success-condition comparator (eq, lt, gt, etc.) against the returned value.
3. Writes a timestamped CSV line (`zulu_ts, status_string, value`) to a log file in a git
   repo (`slaclab/s3df-status`).
4. Commits + pushes the change (consumed by Fettle dashboard).

### Relevant queries (from Makefile live tests):

| Backend    | Query                                                                                     | Meaning                      |
|------------|-------------------------------------------------------------------------------------------|------------------------------|
| Prometheus | `avg(avg_over_time(nmap_port_state{service='ssh',group='s3df'}[5m]))`                     | SSH gateway reachability     |
| InfluxDB   | `SELECT mean("status_code") FROM "monit_process" WHERE ("service" = 'slurmctld' OR "service" = 'slurmdbd') AND time > now()-5m GROUP BY "service"` | Slurm daemon health          |

### Data sources at S3DF:

- **Prometheus**: `https://prometheus.slac.stanford.edu` — live probe metrics (nmap, etc.)
- **InfluxDB**: `https://influxdb.slac.stanford.edu`, database `telegraf` — system/service
  metrics via Telegraf agents (monit, slurm, storage).

---

## 5. Proposed Design

### 5.1 Resource Registry (Static Config)

Resources are *configuration*, not dynamic discovery. We define them via a YAML/dict
config that maps S3DF infrastructure to IRI `Resource` objects. Example:

```yaml
resources:
  - id: "s3df-ssh-gateway"
    name: "SSH Login Gateway"
    group: "access"
    resource_type: "service"
    capability_ids: []
    health_check:
      backend: prometheus
      query: "avg(avg_over_time(nmap_port_state{service='ssh',group='s3df'}[5m]))"
      condition: {op: "eq", value: 1.0}

  - id: "s3df-slurmctld"
    name: "Slurm Controller"
    group: "compute"
    resource_type: "compute"
    capability_ids: []
    health_check:
      backend: influxdb
      db_name: "telegraf"
      query: "SELECT mean(\"status_code\") FROM \"monit_process\" WHERE \"service\" = 'slurmctld' AND time > now()-5m"
      condition: {op: "eq", value: 1.0}
```

This maps directly to the pattern `status-pusher` uses: query + comparator → up/down.

### 5.2 Health Evaluation (Reused from status-pusher)

```
  ┌──────────────────────────────────────────────────────────────┐
  │              HealthChecker (internal module)                  │
  │                                                              │
  │  prometheus_query(url, query) -> (epoch_ts, value)           │
  │  influx_query(url, db, query) -> (epoch_ts, value)           │
  │  evaluate(value, condition) -> Status (up|down|degraded)     │
  └──────────────────────────────────────────────────────────────┘
```

This directly reuses the logic from `status_pusher.py`:
- `prometheus_query()` → wraps `PrometheusConnect.custom_query()`
- `influx_query()` → HTTP GET to InfluxDB `/query` endpoint
- `evaluate()` → applies comparator (eq/lt/gt/gte/lte) to determine status

### 5.3 Caching / Polling Strategy

Querying backends on every HTTP request would be expensive and fragile. Two options:

**Option A: Background Poller (Recommended)**
```
  ┌─────────────────────────────────────────────────────────────┐
  │  BackgroundPoller (asyncio.Task, runs at startup)            │
  │                                                             │
  │  Every N seconds (configurable, default 60):                │
  │    for resource in config.resources:                        │
  │      result = health_check(resource)                        │
  │      cache[resource.id] = StatusSnapshot(ts, status, value) │
  │      if status_changed:                                     │
  │        events.append(new Event)                             │
  └─────────────────────────────────────────────────────────────┘
```
- Cache is an in-memory dict protected by asyncio Lock
- Events are accumulated in-memory (optionally persisted to SQLite/file for restart)
- `get_resources()` reads from cache
- `current_status` on each Resource reflects the latest polled value

**Option B: On-demand with TTL Cache**
- Simpler; queries backend on first request, caches for TTL seconds
- Risk: slow/failed backend blocks API response

### 5.4 Events & Incidents

Events are generated when status *transitions* occur (e.g., up→down). The poller detects
transitions by comparing the new status to the cached previous status.

```
  Time ──────────────────────────────────────────────►

  Resource: s3df-ssh-gateway
  Poll 1: status=up       (no event)
  Poll 2: status=up       (no event)
  Poll 3: status=down     → Event(occurred_at=now, status=down)
                           → Incident(type=unplanned, start=now, status=down)
  Poll 4: status=down     (no event, incident still open)
  Poll 5: status=up       → Event(occurred_at=now, status=up)
                           → Incident.end=now, resolution=completed
```

Incidents are auto-created for unplanned outages. Planned incidents can be injected via:
- Future: a maintenance-schedule config or API
- For MVP: all incidents are `unplanned` and auto-generated

### 5.5 Module Structure

```
app/s3df/
├── status_adapter.py          # S3DFStatusAdapter class
├── status/
│   ├── __init__.py
│   ├── config.py              # Resource registry + env-driven settings
│   ├── health_checker.py      # Prometheus/InfluxDB query + evaluation
│   ├── poller.py              # Background polling loop
│   └── store.py               # In-memory event/incident store
└── ...
```

### 5.6 Data Flow (Request Path)

```
  Client GET /status/resources
       │
       ▼
  status.py router
       │
       ▼
  S3DFStatusAdapter.get_resources(filters...)
       │
       ├─► reads resource list from config (static)
       ├─► enriches current_status from cache (poller-written)
       ├─► applies filters (name, group, type, status, modified_since, etc.)
       └─► returns paginated list
```

```
  Client GET /status/events?resource_id=X&from=T1&to=T2
       │
       ▼
  S3DFStatusAdapter.get_events(filters...)
       │
       ├─► reads event log from in-memory store
       ├─► applies filters (resource_id, status, time range, etc.)
       └─► returns paginated list
```

---

## 6. Configuration (Environment Variables)

| Variable                   | Purpose                                    | Default                            |
|----------------------------|--------------------------------------------|------------------------------------|
| `S3DF_PROMETHEUS_URL`      | Prometheus endpoint                        | `https://prometheus.slac.stanford.edu` |
| `S3DF_INFLUXDB_URL`        | InfluxDB endpoint                          | `https://influxdb.slac.stanford.edu`   |
| `S3DF_INFLUXDB_DB`         | InfluxDB database name                     | `telegraf`                         |
| `S3DF_STATUS_POLL_INTERVAL`| Seconds between health checks              | `60`                               |
| `S3DF_STATUS_CONFIG_PATH`  | Path to resource registry YAML (optional)  | built-in defaults                  |

---

## 7. Comparison: status-pusher vs Status Adapter

| Aspect              | status-pusher                    | S3DF Status Adapter                    |
|---------------------|----------------------------------|----------------------------------------|
| Runtime model       | Cron job / k8s CronJob           | Long-running FastAPI background task   |
| Output              | CSV in git repo → Fettle         | In-memory cache → IRI REST API         |
| Query backends      | Prometheus, InfluxDB             | Same (reuse query logic)               |
| Evaluation logic    | comparator(value, threshold)     | Same (reuse evaluation logic)          |
| Event tracking      | None (single point-in-time)      | Stateful: detects transitions          |
| Incident tracking   | None                             | Auto-created on status transitions     |
| Auth                | N/A (runs internally)            | Unauthenticated (status is public)     |

---

## 8. Implementation Steps

1. **Create `app/s3df/status/health_checker.py`** — Extract and adapt
   `prometheus_query()` and `influx_query()` from `status-pusher`, adding async support
   (use `httpx` instead of `requests` for non-blocking I/O). Port the condition evaluator.

2. **Create `app/s3df/status/config.py`** — Define the resource registry as a Python
   dict/dataclass (with optional YAML override). Include health check definitions per
   resource.

3. **Create `app/s3df/status/store.py`** — In-memory store for Events and Incidents with
   append-only log semantics. Provides filtered query methods matching the adapter
   interface.

4. **Create `app/s3df/status/poller.py`** — Background asyncio task that periodically
   runs health checks, updates resource status, and emits Events/Incidents on transitions.

5. **Create `app/s3df/status_adapter.py`** — `S3DFStatusAdapter` class implementing
   `app.routers.status.facility_adapter.FacilityAdapter`. Wires together config, cache,
   and store. Starts poller on first access or app startup.

6. **Register** — Set `IRI_API_ADAPTER_status=app.s3df.status_adapter.S3DFStatusAdapter`
   in Dockerfile/deployment config.

7. **Tests** — Unit tests mirroring `status-pusher/test/` patterns: mock Prometheus and
   InfluxDB responses, verify status evaluation, event generation, and adapter filtering.

---

## 9. Open Questions

- **Persistence across restarts**: Should events/incidents survive process restarts?
  Options: SQLite file, Redis, or accept ephemeral (events regenerate from live state).
- **Planned maintenance**: How should planned incidents be ingested? Config file, external
  API call, or manual injection?
- **Fettle coexistence**: Should this adapter *replace* the status-pusher→Fettle pipeline
  or run alongside it? (IRI could become the single source of truth for both.)
- **Additional resources**: What is the full set of resources to monitor? (SSH gateway,
  slurmctld, slurmdbd, storage, network, web services, etc.)
- **Degraded vs Down thresholds**: Should we support a third threshold for `degraded`
  status (e.g., partial failure), or only binary up/down per status-pusher's model?

---

## 10. Dependencies

- `prometheus-api-client` (already used by status-pusher)
- `httpx` (async HTTP for InfluxDB queries — preferred over sync `requests` in async app)
- No new external services required; uses existing Prometheus + InfluxDB infrastructure
