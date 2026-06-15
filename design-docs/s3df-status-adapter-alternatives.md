# S3DF Status Adapter — Alternative Approaches Evaluation

> Companion to `s3df-status-adapter.md`. That document describes the **as-built**
> adapter (in-process background poller + in-memory store). This document
> evaluates **alternative implementations** against the goals that matter for this
> service: low **API latency**, **simplicity**, **maintainability**,
> **extensibility**, and **scalability** (concurrent requests serviceable).

---

## 1. Grounding: how this service is actually served

```
  gunicorn.config.py:
    workers = 8
    worker_class = "uvicorn.workers.UvicornWorker"
```

Production runs **8 independent worker processes** (the Dockerfile's `fastapi run`
is single-process, but the gunicorn config is the deployment target). This single
fact dominates the design trade-offs:

```
            ┌──────────── load balancer ────────────┐
            │        │        │      ...      │       │
          worker1  worker2  worker3   ...   worker8
            │        │        │               │
   each one: its OWN in-memory store, its OWN poller,
             its OWN incident/event UUIDs
```

With the current poller+store design, at 8 workers you get:

- **8× backend polling load** (every worker scrapes Prometheus/InfluxDB on its own
  timer), and
- **8 divergent histories** — a client hitting `/status/incidents` gets a different
  answer depending on which worker the load balancer picked, with different
  incident IDs for the *same* outage.

So the real question is **not** "poller vs on-demand" — it is **"where do current
status and history actually live?"** Everything below is organized around that.

---

## 2. The options at a glance

| # | Approach | Request-path I/O | History lives in | New deps | Multi-worker consistent |
|---|----------|------------------|------------------|----------|-------------------------|
| **A** | Background poller + in-memory store *(current/as-built)* | none (RAM read) | process memory | none | ❌ diverges ×8 |
| **B** | On-demand query + TTL cache | backend on cache-miss | nowhere (current only) | none | ⚠️ current-only |
| **C** | **Prometheus as source of truth** + thin TTL cache | 1–2 Prom queries / miss | Prometheus TSDB | none | ✅ all read same TSDB |
| **D** | Read dashboard git logs *(rejected)* | git pull (cached) | git CSV logs | none | ✅ shared clone |
| **E** | **Proxy Alertmanager** (incidents) + Prom instant (status) | Alertmanager / Prom | Alertmanager | none\* | ✅ |
| **F** | Single poller → shared Redis/SQLite, workers read | cache read | shared store | Redis/SQLite | ✅ |

\* Conditional on Alertmanager being deployed — standard alongside Prometheus, but
needs confirmation at S3DF.

---

## 3. Per-approach review

### A — Background poller + in-memory store (as-built)

The data plane is built in-process: registry + async health checker + in-memory
store + background poller + a transition state machine + lazy lifecycle.

- **Latency:** best possible — request handlers do pure in-RAM dict/list filtering,
  no I/O on the hot path.
- **Concurrency:** highest (see §4).
- **Simplicity:** lowest — most moving parts of any option.
- **Maintainability:** the transition/incident state machine and the lazy
  start/shutdown lifecycle are the parts most likely to harbor bugs.
- **Extensibility:** adding a resource = one registry entry (good); changing
  *semantics* (flap suppression, planned maintenance) means editing the state
  machine.
- **Correctness:** weakest at 8 workers — volatile (lost on restart), 8× scrape
  load, and divergent per-worker histories/IDs.

> The complexity in A exists **only** to maintain history inside the process. If
> history can live somewhere that already persists it, most of this code deletes.

### B — On-demand query + TTL cache

Drop the store and poller; on each request (cache-miss) query the backend for the
current value, cache the result for N seconds.

- **Latency:** good when warm; a backend round-trip on cold/expired cache.
- **Simplicity:** much less code than A.
- **Fatal limitation:** **`/events` and `/incidents` become meaningless.** With no
  polling, an outage that starts and ends between requests is never observed —
  there is no continuous sampling to detect transitions.
- **Verdict:** only viable if the API is narrowed to **current status only**.

### C — Prometheus as the source of truth (recommended default)

Prometheus already stores the time-series **and** its history. Derive everything
from it instead of re-implementing a store:

```
  GET /status/resources   ──► Prometheus INSTANT query  (/api/v1/query)
                               one query, labels return ALL resources at once
                                   │
                                   ▼  current_status per resource

  GET /status/events      ──► Prometheus RANGE query    (/api/v1/query_range)
                               fold the series → detect transitions on the fly
                                   │
                                   ▼  Event list (derived)

  GET /status/incidents   ──► same RANGE data → collapse sustained "down"
                               windows into Incidents (start = first down,
                               end = recovery)
                                   │
                                   ▼  Incident list (derived)

  ── all wrapped in a 15–30s TTL response cache ──
```

- **Latency:** near-RAM in the common case (served from the TTL cache); a small,
  bounded number of Prometheus queries on a miss.
- **Simplicity:** **large reduction** — no store, no poller, no transition
  *machine to maintain as state*; just a query builder + a pure transition fold +
  a cache. The transition logic becomes a **pure function over data**, which is far
  easier to test than a long-lived mutating state machine.
- **Maintainability/extensibility:** new resource = new query/labels; semantics
  (e.g. "down for ≥ 2 samples") are expressed as query/fold parameters.
- **Scalability/consistency:** all 8 workers read the **same** TSDB → identical
  answers, stable IDs (derive incident IDs deterministically from
  resource+window). Survives restarts for free. Backend load is bounded by the
  cache, not by request rate.
- **Trade-off:** event/incident fidelity is bounded by Prometheus **scrape
  resolution and retention**; incident IDs must be derived deterministically (not
  random) to stay stable across workers and refreshes.

### D — Reuse dashboard git logs (rejected)

The dashboard pipeline writes append-only CSV health logs that are consumed by
the S3DF status site. The adapter could clone/pull that repo and parse the logs.

- **Pros:** zero new backend access; reuses a pipeline that already runs;
  worker-consistent via a shared clone; history already persisted in git.
- **Cons:** git-pull latency on refresh; a thin point-in-time `ts,status,value`
  data model with **no native incident semantics** (would still need a transition
  fold like C, but over coarser data); couples IRI to the dashboard's repo layout.
- **Verdict:** rejected for IRI runtime use. IRI must remain decoupled from
  `status-pusher`, `s3df-status`, and dashboard log repositories; direct
  IRI-configured checks are the supported polling source.

### E — Proxy Alertmanager for incidents (best incident fidelity)

If S3DF runs **Alertmanager** (the standard companion to Prometheus), it already
models exactly what IRI's status API wants:

```
  Alertmanager alert (firing → resolved)   ≈   IRI Event / Incident
  Alertmanager grouping / dedup            ≈   one Incident, many Events
  Alertmanager SILENCE (maintenance window) ≈   IRI PLANNED Incident
```

- **Pros:** removes nearly all custom incident logic; gives **planned incidents**
  (via silences) that A/B/C cannot produce; dedup/grouping handled upstream;
  consistent across workers.
- **Cons:** depends on Alertmanager being deployed and on alert rules existing for
  S3DF resources; current status may still come from a Prometheus instant query.
- **Verdict:** **highest-leverage path for `/events` + `/incidents`** if
  Alertmanager is available — pair with a Prometheus instant query for
  `current_status`. Confirm availability first.

### F — Single poller → shared store, workers read

Keep A's model but **decouple polling from the 8 workers**: one writer (a
dedicated poller task/process or k8s sidecar/CronJob) populates a shared
Redis/SQLite; all workers only read it.

- **Pros:** keeps A's fast, RAM-like read latency; fixes divergence (1 writer, N
  readers); 1× backend load instead of 8×; survives restarts (if persisted).
- **Cons:** adds an external store dependency and a deployment component.
- **Verdict:** the right evolution **if** the poller model must be retained.

---

## 4. Scalability — "how many concurrent requests can we serve?"

The deciding factor is whether the **request path performs remote I/O**:

```
  ── RAM / cache hit  (A,  C-warm,  D-warm,  F) ─────────────────────────
     handler ≈ microseconds of dict/list filtering
       • ceiling = CPU × 8 workers → thousands of req/s on the event loop
       • BACKEND load is CONSTANT, independent of request rate

  ── Remote on the request path  (B-cold,  C-cold,  E) ──────────────────
     per-request latency = backend round-trip (~10–50 ms Prom instant)
       • concurrency bounded by httpx pool size + backend capacity
       • BACKEND load SCALES with request rate (burst risk / thundering herd)
```

Implications:

- **A** maximizes concurrency, but pays with **8× backend load** and inconsistent
  data.
- **C with a short TTL cache** achieves **the same warm-path concurrency as A**
  (reads served from cache), while collapsing backend load to a steady trickle and
  staying consistent. **The cache is what lets C scale like A without A's state
  machine.**
- **B-cold / C-cold / E** are bounded by the backend; protect them with the TTL
  cache + single-flight (coalesce concurrent misses into one backend call) to avoid
  a thundering herd when the cache expires.

Rule of thumb: keep the **hot path in-memory/cache**, keep **backend load
decoupled from request rate**, and the service comfortably saturates the 8 workers'
CPU before any backend becomes the bottleneck.

---

## 5. Recommendation (tiered)

1. **Immediate, lowest-effort win (keep current code):** decouple polling from the
   workers — run a **single** poller (option **F-lite**) or set this route's
   workers to 1 — to stop the 8× scrape load and per-worker divergence **today**.

2. **Best balance of simplicity + latency + consistency:** **migrate to C —
   Prometheus as the source of truth + a 15–30s TTL cache (with single-flight).**
   Deletes the store, poller, and stateful transition machine; fixes restart-loss
   and worker divergence; reuses infrastructure already operated; holds latency
   near-RAM via the cache.

3. **For real incidents + planned maintenance:** **investigate E (Alertmanager).**
   If deployed, proxy it for `/events` + `/incidents` (silences → planned
   incidents) and keep a Prometheus instant query for `current_status`.

### Net

The in-memory machinery in the as-built adapter is the thing to **remove**.
Prometheus — and likely Alertmanager — already provide persistence, history, and
cross-worker consistency. Pushing state into them **simplifies the code** *and*
**improves scalability**, while a TTL cache preserves the low API latency that
the in-memory design was chosen for in the first place.

---

## 6. Decision checklist (to unblock a direction)

- [ ] Is direct **Prometheus** query access allowed from the IRI API pods? → enables **C**.
- [ ] Is **Alertmanager** deployed, with alert rules covering S3DF resources? → enables **E**.
- [ ] Must `/events` and `/incidents` be meaningful (vs. current-status-only)? → rules out **B**.
- [ ] Is adding **Redis/SQLite** acceptable operationally? → enables **F**.
- [ ] Required event/incident **fidelity** vs. Prometheus scrape interval & retention?
- [ ] Target **request rate / concurrency** and acceptable **p99 latency** under cache-miss?
