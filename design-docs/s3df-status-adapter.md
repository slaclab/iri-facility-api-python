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

---

> **As-built addendum.** Sections 11–12 below describe the logic actually
> implemented in `app/s3df/status_adapter.py`. The adapter ships as a **single
> file** (registry + health checker + store + poller + adapter class), not the
> multi-module layout sketched in sections 5/8. Prometheus is queried directly
> over its HTTP API via `httpx` (the `prometheus-api-client` dependency was not
> needed).

## 11. Status Computation (As Built)

### 11.1 End-to-end pipeline

A resource's `current_status` is derived once per poll cycle by turning a single
scalar metric into one of four `Status` values. The same pipeline runs for every
resource in `RESOURCE_REGISTRY`.

```
  ResourceDef.health_check
  ┌───────────────────────────────────────────────────────────────────────┐
  │ backend (prometheus|influxdb) + query + up_when [+ degraded_when]      │
  └───────────────────────────────────────────────────────────────────────┘
            │
            ▼  HealthChecker.check()
   ┌──────────────────┐     query backend over httpx (timeout-bounded)
   │  Prometheus       │──►  GET /api/v1/query?query=...
   │   or InfluxDB     │──►  GET /query?q=...&db=...
   └──────────────────┘
            │
            ▼  parse a single scalar
        value: float | None        (None = empty result / parse miss)
            │
            ▼  evaluate(check, value)
        ┌───────────────────────────┐
        │  Status:                  │
        │   up | degraded | down |  │
        │   unknown                 │
        └───────────────────────────┘
            │
            ▼  StatusStore.record(resource_id, HealthResult)
     update current_status, emit Event on change, open/close Incident
```

### 11.2 Value → Status rule

`evaluate()` applies the resource's conditions in a fixed precedence. A
`Condition` is just a comparator (`eq/ne/lt/lte/gt/gte`) applied to the observed
value against a threshold — the same success-criterion idea `status-pusher` uses.

```
  value is None  ───────────────────────────────►  UNKNOWN
        │  (query failed, errored, empty result,
        │   or backend mapped to no scalar)
        ▼
  up_when.met(value)?  ── yes ──────────────────►  UP
        │ no
        ▼
  degraded_when set AND degraded_when.met(value)? ─ yes ──►  DEGRADED
        │ no
        ▼
                                                    DOWN
```

Notes:
- **`unknown` ≠ `down`.** Any exception during the query (timeout, TLS, HTTP
  error, malformed JSON) is caught and mapped to `unknown`, *not* `down`. A
  monitoring-plane failure is not treated as a confirmed resource outage.
- **`degraded` is optional.** Resources that only define `up_when` collapse to a
  binary up/down model (matching `status-pusher`). `degraded_when` is the hook
  for a future third threshold.
- The registry currently uses `up_when = (value == 1.0)` for all resources
  (nmap port state for SSH; monit `status_code` for slurmctld/slurmdbd).

### 11.3 Transition detection & events

The store is **change-driven**: `record()` compares the new status to the
resource's previous status and does nothing on a steady state. Events are emitted
only on a real change, which keeps the event log meaningful instead of one row
per poll.

```
  poll cycle N:   prev = store.current_status[r]
                  new  = result.status

      prev == new  ────────────────►  no-op (steady state)

      prev != new  ────────────────►  emit Event(occurred_at = poll time,
                                                  status = new)
                                       + drive incident state machine (11.4)

  Special case: prev is None (first ever observation)
      → "baseline" Event describing the initial status
      → if initial status is down/degraded, an incident is ALSO opened
        (its start time is the first-observed time, not the true outage start)
```

Example for one resource over six polls:

```
  poll:   1     2     3     4     5     6
  status: up    up    down  down  up    unknown
  event:  base  —     E1    —     E2    E3
                      ▲           ▲     ▲
                      │           │     └ up→unknown (incident stays open? no —
                      │           │       it was already closed at poll 5)
                      │           └ down→up  (closes incident, resolution=completed)
                      └ up→down   (opens unplanned incident)
```

### 11.4 Incident lifecycle (state machine)

At most **one open incident per resource**. Incidents are auto-created as
`unplanned`. The driving signal is the resource's status transition:

```
                       ┌───────────────────────────────────────────┐
                       │                                           │
        up / (start)   │                                           │
   ───────────────►  (NO OPEN INCIDENT)                            │
                       │   │                                       │
        down|degraded  │   │  down|degraded                        │ up
                       │   ▼                                       │
                     (OPEN: unplanned, resolution=unresolved)      │
                       │   │   ▲                                   │
            down⇄degraded   │   │ down|degraded (escalate:         │
        (update incident    │   │   update incident.status only)   │
         status + mtime)    │   └───────────────────────────────  │
                       │   │                                       │
                       │   │  up  ─────────────────────────────────┘
                       │   ▼     close: end=now,
                       │  (CLOSED: resolution=completed, status=up)
                       │
        unknown ───────┘  (incident is LEFT OPEN; only an event is recorded —
                           a backend failure must not auto-resolve an outage)
```

Concretely, in `record()`:
- **→ down/degraded:** open a new incident if none is open for the resource;
  otherwise update the existing incident's `status`. Link the new event to the
  incident (`event.incident_id`, `incident.event_ids`).
- **→ up:** pop and close the open incident (`end`, `resolution=completed`,
  `status=up`), linking the closing event.
- **→ unknown:** record the event only; never opens or closes an incident.

### 11.5 Polling cadence & lazy start

IRI constructs adapters **synchronously at import time**, before the asyncio loop
exists, so the poller cannot start in `__init__`. Instead it starts lazily on the
first request, guarded by a double-checked `asyncio.Lock`.

```
  import time            first request (loop running)        steady state
  ───────────────►       ─────────────────────────►          ───────────►

  __init__:              _ensure_started():                  background task:
   build store,           async with start_lock:              loop forever:
   poller, lock           if not started:                       sleep(interval)
   (NO network,            • create httpx.AsyncClient            poll_once()
    NO task)               • await poll_once()  ◄─ initial         └ gather all
                              (populates store so the              checks, record
                               first response isn't empty)
                            • create_task(_run())
                            • started = True
                          ── request proceeds, reads store ──
```

- The **initial poll is awaited** under the lock, so the very first
  `/status/...` response reflects real data rather than all-`unknown`. Its cost is
  bounded by per-query `S3DF_STATUS_HTTP_TIMEOUT`, and `check()` never raises
  (failures become `unknown`).
- All resource checks within a cycle run concurrently via `asyncio.gather`.
- `aclose()` cancels the loop and closes the HTTP client (for tests / future
  FastAPI-lifespan wiring).

## 12. Limitations & Known Trade-offs

These are intentional simplifications for a first reviewable implementation. Each
has a clear upgrade path.

### 12.1 Volatile, per-process state

State (current status, events, incidents) lives in plain Python structures. It is
**lost on restart** and **diverges across uvicorn workers** — each worker polls
independently and mints its own incident/event UUIDs, so a client may see
different histories depending on which worker answers.

```
        ┌── worker A ──┐        ┌── worker B ──┐
        │ store_A      │        │ store_B      │
        │ inc id=abc   │   ≠    │ inc id=xyz   │   ← same outage, different ids
        └──────────────┘        └──────────────┘
   GET /status/incidents → load-balanced → answer depends on worker
```

Upgrade path: shared store (SQLite/Redis/Postgres) or a single dedicated poller
process writing a store the API workers only read.

### 12.2 Single sample, no flap suppression

Status is decided from **one** sample per cycle with no debounce/hysteresis. A
single transient bad scrape flips the resource and generates an event/incident;
the next good poll closes it. A flapping metric produces incident churn.

```
  value:  1   1   0   1   0   1     (0 = "bad" scrape)
  status: up  up  dn  up  dn  up
  events:     —   E   E   E   E     ← four events, two short incidents
```

Upgrade path: require N consecutive failures before opening (and N successes
before closing), or evaluate over a rolling window.

### 12.3 `unknown` and unknown outage-start

- A backend/query failure yields `unknown`, which deliberately does **not** close
  an open incident — but a long backend outage will leave a resource pinned at
  `unknown` with no incident of its own.
- When a resource is **already down at first observation**, the incident `start`
  is set to *first-observed time*, which is not the true outage start (the
  adapter has no history before it booted).

### 12.4 Simplistic query → scalar reduction

Each backend response is reduced to a single number: Prometheus
`data.result[0].value[1]`, InfluxDB `series[0].values[-1][1]`. Queries that return
multiple series / grouped results (e.g. `GROUP BY service`) only have their
**first** series considered. Health checks must be written to return a single
series. There is no "any/all/aggregate across series" policy yet.

### 12.5 Scope gaps

- **No planned incidents.** All incidents are `unplanned`; there is no ingestion
  of maintenance windows (`planned` / `reservation`). Planned-maintenance support
  needs a schedule source (config or API).
- **`site_id` is not a guaranteed cross-reference.** Resources carry
  `S3DF_SITE_ID` (default `"s3df"`), but the `/facility` adapter currently mints a
  random site UUID per process, so `Resource.site_uri` may not resolve to a real
  site until a stable site id is wired through.
- **Resource set is minimal.** Only SSH gateway + slurmctld + slurmdbd are
  registered today; the full S3DF resource catalogue (storage, network, web
  services, …) still needs to be enumerated.

### 12.6 Operational

- **Unbounded event growth.** The event log is append-only and never trimmed;
  a long-lived process accumulates events in memory. Needs retention/eviction
  (or persistence with TTL).
- **First-request latency.** The first caller after startup pays the awaited
  initial poll (up to `S3DF_STATUS_HTTP_TIMEOUT` per backend round).
- **TLS verification defaults off.** `S3DF_STATUS_TLS_VERIFY` defaults to `false`
  (SLAC internal CA convenience); set it to `true` or a CA-bundle path in
  production.
- **No automatic shutdown wiring.** `aclose()` exists but the framework does not
  call it; wiring it into FastAPI's lifespan is a follow-up so the poller task and
  HTTP client are closed cleanly on shutdown.

