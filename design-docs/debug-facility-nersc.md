# Debug Report: `/facility` Endpoints Returning "NERSC" Data

**Date:** 2026-05-21  
**Branch:** `feat/user-lookup-integration`

---

## Summary

Even with `IRI_API_ADAPTER_facility` correctly set to `S3DFFacilityAdapter`, NERSC references leak into `/facility` responses through **three separate mechanisms**: the `API_URL_ROOT` fallback, the OpenAPI schema, and the FastAPI server definition. The S3DF adapter itself returns clean SLAC data, but the surrounding framework still defaults to NERSC.

---

## Hardcoded NERSC Couplings

### 1. `API_URL_ROOT` fallback poisons all URI fields

**File:** `app/config.py:39`
```python
API_URL_ROOT = os.environ.get("API_URL_ROOT", "https://api.iri.nersc.gov")
```

**Impact chain:**
- `app/request_context.py:30` — `get_url_prefix()` returns `API_URL_ROOT` when no `x-forwarded-host` header is present:
  ```python
  return f"{config.API_URL_ROOT}{config.API_PREFIX}{config.API_URL}"
  # → "https://api.iri.nersc.gov/api/v1"
  ```
- `app/types/base.py:61` — every `NamedObject.self_uri` computed field uses `get_url_prefix()`:
  ```python
  return f"{get_url_prefix()}{self._self_path()}"
  # → "https://api.iri.nersc.gov/api/v1/facility"
  ```
- `app/routers/facility/models.py:27` — `Site.resource_uris` uses `get_url_prefix()`
- `app/routers/facility/models.py:54` — `Facility.site_uris` uses `get_url_prefix()`

**Result:** If `API_URL_ROOT` env var is not set (e.g., local dev without `local.env`, or a deployment missing this var), every `self_uri`, `site_uris`, and `resource_uris` field in the response contains `nersc.gov`.

---

### 2. FastAPI `servers` definition hardcodes NERSC

**File:** `app/main.py:51`
```python
APP = FastAPI(servers=[{"url": config.API_URL_ROOT}], **config.API_CONFIG)
```

The OpenAPI `servers` array (visible at `/openapi.json` and the Swagger "Try it out" dropdown) defaults to `https://api.iri.nersc.gov`. Any auto-generated client or Swagger UI will target NERSC by default.

---

### 3. Pydantic `example` values reference NERSC/LBL

**File:** `app/routers/facility/models.py`

| Line | Field | Example Value |
|------|-------|---------------|
| 13 | `Site.short_name` | `"NERSC"` |
| 14 | `Site.operating_organization` | `"Lawrence Berkeley National Laboratory"` |
| 16 | `Site.locality_name` | `"Berkeley"` |
| 17 | `Site.state_or_province_name` | `"California"` |
| 18 | `Site.street_address` | `"1 Cyclotron Rd"` |
| 22 | `Site.latitude` | `37.8762` (Berkeley, CA) |
| 23 | `Site.longitude` | `-122.2506` (Berkeley, CA) |

These appear in: OpenAPI spec, Swagger UI examples, and any client SDK generated from the schema.

---

### 4. OpenTelemetry resource attributes

**File:** `app/main.py:35`
```python
resource = Resource.create({
    "service.name": "iri-facility-api",
    "service.version": config.API_VERSION,
    "service.endpoint": config.API_URL_ROOT  # ← nersc.gov
})
```

Traces exported to any collector will carry NERSC's endpoint in `service.endpoint`.

---

### 5. `S3DFFacilityAdapter.self_uri` hardcoded (minor)

**File:** `app/s3df/facility_adapter.py:47`
```python
self_uri="https://s3df-dev.slac.stanford.edu/api/v1/facility"
```

This is a Pydantic `extra` field passed at construction time. However, it's **overridden** by the computed `self_uri` property on `NamedObject` (which calls `get_url_prefix()`). So the value set here is effectively dead code — the serialized response uses the computed value. But it adds confusion during debugging.

---

### 6. README and tools reference NERSC

| File | Line | Content |
|------|------|---------|
| `README.md` | 6-8 | NERSC instance URLs in docs |
| `tools/globus.py` | 55 | `"NERSC-linked Globus identity"` |
| `test/test_filesystem.py` | 17 | Commented-out NERSC base URL |

---

## Why the Problem Manifests

The `dev-s3df` Makefile target (line 42-46) sets `API_URL_ROOT='http://127.0.0.1:8000'` but does **NOT** set `IRI_API_ADAPTER_facility`. The Dockerfile (line 10-11) sets `IRI_API_ADAPTER_account` and `IRI_SHOW_MISSING_ROUTES=true` but also does **NOT** set `API_URL_ROOT`.

So in a deployed container:
1. `IRI_API_ADAPTER_facility` is set externally → S3DF adapter loads ✓
2. `API_URL_ROOT` is **not** set → defaults to `https://api.iri.nersc.gov` ✗
3. No `x-forwarded-host` header (direct access, no gateway) → `get_url_prefix()` falls back to `API_URL_ROOT` ✗
4. All computed URIs in the response contain `nersc.gov` ✗

---

## All Affected Files

| File | Line(s) | Coupling Type |
|------|---------|---------------|
| `app/config.py` | 39 | `API_URL_ROOT` default |
| `app/main.py` | 51 | FastAPI `servers` array |
| `app/main.py` | 35 | OTel `service.endpoint` |
| `app/request_context.py` | 30 | `get_url_prefix()` fallback |
| `app/routers/facility/models.py` | 13-23 | Pydantic examples (NERSC/Berkeley) |
| `app/s3df/facility_adapter.py` | 47 | Dead `self_uri` param |
| `tools/globus.py` | 55 | NERSC Globus instruction |
| `README.md` | 6-8 | NERSC instance docs |
| `Dockerfile` | (missing) | No `API_URL_ROOT` override |

---

## Recommended Fixes

1. **`app/config.py:39`** — Change default to a neutral value:
   ```python
   API_URL_ROOT = os.environ.get("API_URL_ROOT", "http://localhost:8000")
   ```

2. **`Dockerfile`** — Add `API_URL_ROOT` env var (set to the actual S3DF deployment URL).

3. **`app/routers/facility/models.py`** — Replace NERSC/Berkeley examples with generic or S3DF-relevant examples.

4. **`app/s3df/facility_adapter.py:47`** — Remove the dead `self_uri` kwarg (it's overridden by the computed property).

5. **`app/main.py:51`** — The `servers` list is already driven by `config.API_URL_ROOT`, so fixing #1 fixes this.

6. **`tools/globus.py:55`** — Generalize the Globus identity instruction or make it configurable.
