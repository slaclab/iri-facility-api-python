# Upstream v2 S3DF Remaining Migrations

## Status

The first-draft integration is on `merge/upstream-v2-s3df`.

| Reference | Value |
| --- | --- |
| Fork baseline | `origin/main@5eab585` |
| Upstream baseline | `upstream/main@7c06e2b` |
| Common ancestor | `a6fcf7e3f50733022350055abddea97f01bae715` |
| Upstream merge commit | `43481bf` |
| Backup branch | `backup/origin-main-pre-upstream-v2-20260715` |
| Preserved local-work stash | `pre-upstream-v2-20260715 local work` |

The merge retained the upstream v2 generic contracts and the bespoke Dex, CoAct, Slurm, status API, user lookup, and fs-facade integrations. No S3DF files were deleted. Direct conflicts and the first semantic compatibility pass are complete.

Tests and live imports were intentionally not run in this environment. Static checks were limited to conflict-marker checks, `git diff --check`, editor diagnostics, generated Slurm client inspection, and dependency-free Python compilation.

## Completed in the first draft

- Upstream v2 URN scalars, filesystem POST bodies, compute project validation, idempotency, status endpoint metadata, and storage router contracts are retained.
- Dex JWT verification and per-user Slurm HS256 JWT brokering are retained.
- The Slurm JWT secret accepts `SLURM_JWT` and the legacy `slurm_jwt` name.
- `X-IRI-Facility-Project` and S3DF authnz headers use independent request contexts and are reset after every request.
- The selected project reaches Slurm submit and update account fields.
- Submit and update return Pydantic jobs where idempotency calls `model_dump()`.
- Live Slurm job details use `job_spec`; single-job slurmdbd fallback and the deliberate historical-list `501` remain intact.
- S3DF status resources include `supported_endpoints`, and the status adapter implements endpoint filtering.
- Facility and site identifiers are stable and use `S3DF_FACILITY_NAME`.
- Compression URNs are translated to fs-facade short names at both filesystem boundaries.
- Public filesystem POST operations retain the existing internal fs-facade verbs.
- The lowercase `app` entry point, proxy-aware request URLs, and root/prefixed logo mounts coexist with upstream `APP` and lifespan startup.
- The S3DF image hides unconfigured routes. `/storage` is therefore hidden until an S3DF adapter exists.
- Redis idempotency documentation now requires both `IRI_IDEMPOTENCY_STORE` and `REDIS_URL`.
- `local-template.env` contains the S3DF adapter, auth, service, stable-ID, and Slurm settings.

## Release blockers

### 1. Install and import the merged dependency set

The merged `pyproject.toml` contains the upstream Redis dependency and the S3DF auth, GraphQL, and bundled slurmrestd dependencies. A configured Python 3.13 environment must still:

1. Resolve/install the dependency union with `uv`.
2. Import `app.main:APP` and `app.main:app`.
3. Instantiate every configured S3DF adapter.
4. Confirm the bundled slurmrestd wheel is copied into and installable in the container build context.
5. Decide whether this repository should add and maintain a lock file; none is currently present.

Editor diagnostics in the current environment only reported unresolved third-party packages such as FastAPI, PyJWT, OpenTelemetry, and the bundled Slurm client. They did not identify a dependency-independent syntax error.

### 2. Configure shared production idempotency

`gunicorn.config.py` declares eight workers. The default in-memory idempotency store is process-local and is not correct for that topology.

Production must set both:

```text
IRI_IDEMPOTENCY_STORE=app.demo_adapter.RedisIdempotencyStore
REDIS_URL=<production Redis URL>
```

Also review `IDEMPOTENCY_TTL_SECONDS` and `LOCK_TTL_SECONDS` against maximum scheduler latency. Verify cache hits, body mismatches, concurrent duplicate requests, lock expiry, and adapter-exception cleanup across multiple workers.

### 3. Decide and enforce project authorization policy

`S3DFAuthenticatedAdapter` verifies Dex tokens, but its CoAct user-membership check remains commented out. Separately, the compute router enforces exactly one account source, but the deployed authorization policy must decide whether Slurm alone is authoritative for charging that account.

Before release:

1. Decide whether every Dex-authenticated user is accepted or must also exist in CoAct.
2. Decide whether CoAct membership must be checked for the selected project before Slurm submit/update.
3. Preserve non-disclosing error behavior for unauthorized projects.
4. Add tests for body account, forwarded project, both sources, neither source, and unauthorized membership.

### 4. Run S3DF service contract checks

The following boundaries require a non-production S3DF environment:

- Dex JWKS, issuer, audience, and username-claim validation.
- CoAct project, capability, allocation, and POSIX identity mapping.
- Slurm submit/update/cancel and live status with per-user headers.
- Slurmdbd single-job historical fallback and ownership defense.
- Status API response merging and full-coverage policy.
- fs-facade task submission, polling, result retrieval, and authnz header forwarding.
- Compression URN conversion for compress/extract.
- Public filesystem POST body to legacy fs-facade verb translation.
- Request-context isolation under concurrent requests.

The generated Slurm v0.0.41 update request model statically includes `account`. The deployed Slurm policy must still confirm that users may update that field as intended.

### 5. Validate the v2 API contract

Generate OpenAPI from the configured S3DF application and run the upstream v2 validator. Confirm:

- Canonical allocation, resource, and compression URNs appear in schemas and responses.
- Compute and filesystem resource endpoints expose only supported resources.
- Filesystem operations are POST requests with request bodies.
- `/storage` is absent while no S3DF storage adapter is configured.
- Proxy-prefixed docs, OpenAPI, `self_uri`, and logo URLs resolve correctly.

## Adapter migrations and decisions

### Account

`Project.last_modified` remains optional upstream. Do not fabricate a current timestamp. If CoAct gains a stable modification timestamp, map it as a timezone-aware value; otherwise continue returning `None`.

Add dedicated S3DF account tests for project membership aggregation, capability allocation URNs, storage/compute allocation mapping, missing CoAct fields, and optional timestamps.

### Compute

- Confirm `SLURM_VERIFY_SSL=true` with the production trust chain. The compatibility default remains `false` to preserve the existing deployment behavior.
- Confirm `SLURM_JWT`, `SLURM_REST_URL`, partition defaults, and account defaults are delivered through the deployment secret/config system.
- Decide whether `SLURM_DEFAULT_ACCOUNT` remains necessary for non-router call paths such as script submission.
- Keep historical job listing at `501` until a bounded slurmdbd query is designed and load-tested.
- Add focused tests for live `job_spec`, project propagation, Pydantic update results, Slurm errors, and historical ownership checks.

### Status and facility

Review the endpoint policy encoded in `status_registry.py` with S3DF operators:

- Slurm partitions are marked for `Endpoint.compute`.
- `sdfhome`, `sdfdata`, and `sdfscratch` are marked for `Endpoint.filesystem`.
- `sdfk8s` has no public endpoint assignment.

Confirm that every registry `site_id` equals the deployed `S3DF_FACILITY_NAME`, that this value will remain stable, and that status coverage behavior is correct for partial service responses.

### Filesystem and task

The task adapter stores IRI-to-fs-facade task IDs and original commands in class-level dictionaries. These mappings are lost on restart and are not shared among workers. Replace them with a shared persistent store or add an fs-facade lookup mechanism before relying on multi-worker/restart-safe task polling.

Also decide whether `S3DFFilesystemAdapter` is a supported direct integration or only a compatibility path beside the task-backed router. Keep it importable while it remains configurable.

### Storage

No `S3DFStorageAdapter` exists. The current deployment decision is to hide `/storage` by leaving `IRI_API_ADAPTER_storage` unset and `IRI_SHOW_MISSING_ROUTES=false`.

If storage becomes release scope, implement:

1. `get_locations()` for S3DF home, project, scratch, campaign, and archive policies.
2. `get_access_endpoints()` for approved Globus, XRootD, or S3 endpoints.
3. POSIX identity and CoAct project authorization for returned locations.
4. Deployment configuration and package exports.
5. Unit, OpenAPI, and service-level tests.

Do not expose the demo storage adapter in an S3DF deployment.

## Deployment completion

- Supply production values for every non-secret setting in `local-template.env`; deliver secrets through the deployment secret manager.
- Set `API_URL_ROOT`, `API_PREFIX`, and `API_URL` for the public S3DF proxy path.
- Keep `IRI_API_ADAPTER_status` configured whenever compute or filesystem is enabled.
- Ensure no deployment override changes `IRI_SHOW_MISSING_ROUTES` to `true`.
- Build the S3DF image and confirm both FastAPI and Gunicorn entry points use the merged lifespan.
- Verify traces and metrics separately, including shutdown of exporters and the idempotency store.
- Retain the backup branch and a pre-v2 image until service smoke tests pass.
- Review the preserved stash, then drop only `pre-upstream-v2-20260715 local work`; do not disturb older unrelated stashes.
- Push `merge/upstream-v2-s3df` and integrate it through the repository's protected-branch review process. No force push is required.

## Validation sequence for the target environment

1. Dependency install and configured-adapter import checks.
2. Focused S3DF account, compute, status, filesystem, task, auth, and request-context tests.
3. Generic upstream test suite.
4. OpenAPI v2 validation.
5. Container build and startup with production-like configuration.
6. Dex, CoAct, Slurm, slurmdbd, status API, and fs-facade smoke tests.
7. Multi-worker Redis idempotency and task-persistence checks.
8. Proxy-path and observability checks.
9. Staged deployment followed by rollback rehearsal.
