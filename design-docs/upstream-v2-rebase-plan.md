# Upstream v2 Integration Plan and S3DF Adapter Impact

## Scope and verified baseline

This plan integrates `doe-iri/iri-facility-api-python` `main` into the SLAC fork while preserving the S3DF implementation and its facility-specific behavior. The implementation used a union-first merge because the fork and upstream both have substantial published history.

Analysis baseline refreshed on 2026-07-15:

| Item | Value |
| --- | --- |
| Authoritative fork main | `origin/main` at `5eab585` |
| Integration branch | `merge/upstream-v2-s3df`; merge commit `43481bf` |
| Local `main` | `653e2a7` (stale; do not use as the rebase base) |
| Upstream main | `7c06e2b` |
| Merge base | `a6fcf7e3f50733022350055abddea97f01bae715` |
| Divergence from `origin/main` | 69 fork-only commits, 60 upstream-only commits |
| Job-history integration | PR #15 squash-merged as `5eab585`; source commits `3d9eb9e` and `626a4ac` must not be replayed |
| Preserved local work | `app/config.py`, `app/main.py`, and this plan in stash `pre-upstream-v2-20260715 local work` |

The GitHub remote is authoritative for the fork. PR #15 is already represented on `origin/main` by squash commit `5eab585`, which contains the job-history changes to `Makefile`, `app/s3df/compute_adapter.py`, and `app/s3df/tests/test_compute_adapter.py`. The source commits remain on `origin/feat/job-history-query`, but are not ancestors of `origin/main` because GitHub used a squash merge.

Upstream has moved to the v2 API contract. The main risk is not deletion of `app/s3df/`: upstream never owned those files, so a rebase should retain the fork additions. The main risk is semantic drift in the generic router interfaces and models inherited by the S3DF classes.

## Required outcome

The rebased branch must:

1. Use upstream v2 router contracts, request validation, URN scalar types, storage routes, and idempotency behavior.
2. Preserve Dex JWT verification and S3DF user resolution.
3. Preserve Slurm JWT brokering and per-user Slurm authorization.
4. Preserve efficient single-job historical lookup through slurmdbd.
5. Preserve the deliberate `501` for historical job listing until slurmdbd load is characterized.
6. Preserve authnz-injected header forwarding to `fs-facade-service`.
7. Preserve the S3DF status microservice plus static resource registry design.
8. Keep the fs-facade wire protocol independent from upstream HTTP route changes.
9. Either implement the new storage adapter or intentionally leave `/storage` hidden and document that decision.

## Chosen integration strategy

Fetch both remotes, then merge upstream into an integration branch created from authoritative `origin/main`. Do not merge the old feature branch and do not replay `3d9eb9e` or `626a4ac`: their combined change is already present in `origin/main` as `5eab585`. Replaying them would duplicate the job-history patch and its conflicts.

### Phase 0: protect the current state

The working tree is already dirty in two files that are also expected conflict points. Preserve those edits before starting.

```bash
git fetch origin --prune
git status --short --branch
git branch backup/origin-main-pre-upstream-v2-20260715 origin/main
git stash push -u -m "pre-upstream-v2-20260715 local work"
```

This path-limited stash leaves the rebase plan in the working tree. Prefer a temporary commit instead of a stash if the uncommitted changes are meaningful work that should be reviewed independently.

Configure and fetch the canonical remote:

```bash
git remote add upstream https://github.com/doe-iri/iri-facility-api-python.git
git fetch upstream --tags
git rev-parse upstream/main
```

If `upstream` already exists, verify its URL instead of adding it again.

### Phase 1: merge upstream main into fork main

Work on an integration branch created directly from the refreshed remote main, leaving local `main`, the merged source branch, and the backup untouched until validation is complete.

```bash
git switch -c merge/upstream-v2-s3df origin/main
git merge --no-ff --no-commit upstream/main
```

During this merge, `ours` is the S3DF fork and `theirs` is upstream. Do not select either side across whole directories: upstream owns generic contracts, while the fork owns S3DF behavior.

Resolution policy:

| Path | Default resolution |
| --- | --- |
| `app/routers/**` | Start from upstream v2, then port only required S3DF integration behavior. |
| `app/types/**` | Take upstream v2 scalar and model contracts. |
| `app/idempotency.py` | Take upstream. |
| `app/s3df/**` | Preserve the fork, then adapt it to the v2 contracts. |
| `app/request_context.py` | Manually combine upstream project context with S3DF auth-header context. |
| `app/main.py` | Manually combine upstream lifespan/storage wiring with S3DF header capture and local logo behavior. |
| `app/config.py` | Start from upstream and reapply reviewed fork settings and the saved local patch. |
| `pyproject.toml` | Keep upstream dependencies and preserve the bundled slurmrestd wheel dependency. |
| `local-template.env` | Keep upstream variables and append all S3DF service/auth/Slurm variables. |
| `Makefile` | Keep upstream targets and reconstruct `dev-s3df` adapter wiring. |

After each conflict group, inspect staged changes before continuing:

```bash
git diff --check
git diff --cached --stat
git diff --name-only --diff-filter=U
```

### Phase 2: complete the v2 S3DF port

Once direct merge conflicts are resolved, make focused semantic changes that Git cannot detect. Apply the changes in this order:

1. Shared request context and application lifespan.
2. Status resource model and endpoint support.
3. Account URNs and required timestamps.
4. Compute project propagation and idempotency return types.
5. Filesystem/task request translation and compression URNs.
6. New storage adapter decision and implementation.
7. Environment, dependency, export, and deployment wiring.
8. S3DF contract tests.

The merged history must contain the net change from `5eab585` exactly once. Resolve its `Makefile`, compute adapter, and compute test changes during the upstream merge. There is no later job-history replay phase.

### Phase 3: restore local work

Reapply the saved `app/config.py` and `app/main.py` changes only after the upstream and S3DF integration versions of those files are stable. Compare manually rather than popping the stash over v2 startup. The merged application preserves the lowercase `app` alias, relative logo URL, root and prefixed logo mounts, proxy URL context, and auth-header context. Keep the stash until review is complete.

### Why merge instead of historical rebase

The fork has 69 fork-only commits and upstream has 60 upstream-only commits after the common base. A merge preserves both published histories, avoids resolving the same generic conflicts repeatedly, and provides one reviewable integration point. The backup branch remains available for tree comparison and rollback.

## High-confidence conflict points

The three-way merge simulation identifies direct overlap in these files:

| Conflict area | Required resolution |
| --- | --- |
| `Makefile` | Preserve upstream commands; rebuild S3DF launch targets and adapter environment variables. |
| `app/config.py` | Preserve upstream v2/telemetry settings and reviewed S3DF deployment settings. Do not overwrite current uncommitted work. |
| `app/main.py` | Keep upstream `APP`, lifespan, idempotency store, and storage router; restore S3DF auth header capture and logo mounts. |
| `app/request_context.py` | Merge upstream `_iri_facility_project` with fork `_auth_headers`; both contexts must be set and reset per request. |
| `app/routers/account/models.py` | Take upstream optional `Project.last_modified` and allocation URN types; update CoAct mapping when a stable timestamp is available. |
| `app/routers/compute/compute.py` | Take upstream resource endpoint, project validation, and idempotency wrappers; preserve S3DF adapter contract. |
| `app/routers/status/status.py` | Take upstream URN query types and endpoint behavior; retain S3DF adapter behind the generic interface. |
| `local-template.env` | Combine upstream idempotency/storage variables with Dex, CoAct, fs-facade, status API, and Slurm settings. |
| `pyproject.toml` | Combine upstream package updates with `app/s3df/slurm/slurmrestd_client-1.0.0-py3-none-any.whl` and its runtime dependencies. |

Likely semantic conflicts without conflict markers:

| Area | Why Git may miss it |
| --- | --- |
| `app/s3df/status_adapter.py` | The inherited interface gains an abstract method in a different file. Instantiation fails only at runtime. |
| `app/s3df/status_registry.py` | Old enum member names still import, but their serialized values change to URNs. |
| `app/s3df/account_adapter.py` | Allocation values serialize as URNs; `last_modified` remains optional and should not be fabricated. |
| `app/s3df/compute_adapter.py` | Header project value is validated in the router but is not passed as a method argument. |
| `app/s3df/compute_adapter.py` | Idempotency calls `.model_dump()` on adapter results; several paths currently return dictionaries. |
| `app/s3df/filesystem_adapter.py` | Upstream removes `get_auth_headers()` even though the S3DF adapter imports it. |
| `app/s3df/task_adapter.py` | Same removed auth context; compression values also change from short strings to URNs. |
| `app/s3df/__init__.py` | New storage adapter and existing filesystem/task adapters need explicit, consistent exports or direct environment paths. |
| Deployment configuration | A missing status adapter now prevents compute/filesystem resource lookup rather than allowing resource IDs through. |

## Data model changes and required migrations

### Canonical URN scalar migration

Upstream v2 replaces several short enum values with DOE IRI URNs and accepts validated extension URNs.

| Model field | Before | Upstream v2 | S3DF action |
| --- | --- | --- | --- |
| `Capability.units[]` | `AllocationUnit`, such as `node_hours` | `AllocationUnitValue`, such as `urn:doe-iri:allocation:compute:node-hours` | Existing enum member references remain source-compatible, but API output and tests must expect URNs. |
| `AllocationEntry.unit` | `AllocationUnit` | `AllocationUnitValue` | Ensure CoAct compute allocations serialize the canonical node-hours URN. |
| `Resource.resource_type` | `ResourceType`, such as `compute` | `ResourceTypeValue`, such as `urn:doe-iri:resource:compute` | Update registry fixtures, filtering assertions, and status microservice translation tests. |
| `PostCompressRequest.compression` | `gzip`, `bzip2`, `xz`, `none` | `urn:doe-iri:compression:<type>` | Translate the public URN back to the short value expected by fs-facade unless that service is upgraded in lockstep. |
| `PostExtractRequest.compression` | Same short values | Same compression URNs | Apply the same boundary translation. |

Do not pass v2 compression `.value` directly to the current fs-facade. After the rebase it will be the full URN, not `gzip`.

### Account models

Verified upstream changes:

- `Project.last_modified` remains optional.
- `AllocationEntry.unit` changes to `AllocationUnitValue`.
- `Capability.units` changes to `list[AllocationUnitValue]`.

Required S3DF changes:

1. Populate `Project.last_modified` from a stable CoAct timestamp if one becomes available; otherwise leave it `None`.
2. Do not use a new current timestamp on every request because that breaks `modified_since` semantics and stable responses.
3. Keep `repo["Id"]`, facility/name construction, and membership aggregation unchanged unless CoAct contract testing shows otherwise.
4. Add account adapter tests; the current S3DF test directory does not contain a dedicated `test_account_adapter.py`.
5. Update expected allocation values to canonical URNs.

### Compute models and request semantics

The `Job`, `JobStatus`, `JobSpec`, and `JobState` field structures are unchanged upstream. The important compute changes are behavioral:

- Submit and update require the effective account in exactly one place: `job_spec.attributes.account` or `X-IRI-Facility-Project`.
- Submit and update accept `Idempotency-Key`.
- Compute exposes `GET /compute/resources`, backed by status `get_resources_for_endpoint(Endpoint.compute)`.
- Compute always resolves `resource_id` through the status adapter. The old fallback that returned the raw ID is removed.

Required S3DF changes:

1. Read the validated forwarded project from request context in `SLACComputeAdapter`, or change the generic handoff so the effective account is explicitly included in the adapter input.
2. Use that value as the Slurm `account`; do not silently fall back to `SLURM_DEFAULT_ACCOUNT` when the request supplied `X-IRI-Facility-Project`.
3. Validate that the authenticated user may charge the selected CoAct project/account before submitting if Slurm authorization alone is not the intended policy.
4. Return `compute_models.Job` from submit, update, get, and list conversion boundaries where upstream wrappers require a model. `run_with_idempotency()` calls `.model_dump()`.
5. Change the live Slurm converter's `"spec"` key to `"job_spec"`. The historical converter already uses the correct field. The current live include-spec value is silently omitted by `IRIBaseModel` serialization.
6. Preserve per-user Slurm JWT issuance, `sun` claim, Slurm headers, state mapping, and the strict generated Slurm request model.
7. Preserve single-job historical fallback and ownership defense-in-depth.
8. Preserve `501` for `get_jobs(..., historical=True)` until a bounded accounting query is available.

Idempotency must be configured with a shared store in multi-worker production. The upstream in-memory default is suitable only for a single process. Add the selected `IRI_IDEMPOTENCY_STORE` and backend connection settings to deployment configuration.

### Status models

Verified upstream changes:

- `ResourceType` moves to canonical URN values in `app/types/scalars.py`.
- `Resource.resource_type` accepts `ResourceTypeValue`, including facility extension URNs.
- `Resource.supported_endpoints: list[Endpoint]` is added with `compute` and `filesystem` values.
- `FacilityAdapter.get_resources_for_endpoint(endpoint)` is a new abstract method.
- The public `site_id` resource-list query is removed, although the internal resource model still contains `site_id`.
- Resource type filtering supports complete URN prefixes.

Required S3DF changes:

1. Add `supported_endpoints` to `ResourceMeta` or derive it in `_build_resource()`.
2. Mark Slurm partition resources with `Endpoint.compute`.
3. Mark resources reachable through fs-facade with `Endpoint.filesystem`; do not mark every storage status record unless filesystem operations are actually valid there.
4. Implement `get_resources_for_endpoint()` using the same status-enriched resource list and filter by endpoint.
5. Update `get_resources()` annotation to `ResourceTypeValue | None` and stop depending on the removed public `site_id` filter.
6. Keep static metadata plus dynamic status merging, event/incident translation, and unknown-status fallback.
7. Ensure `site_id()` matches an actual stable facility site ID. The current facility adapter creates random UUIDs at startup while status defaults to `settings.facility_name` or `s3df`; v2's stronger cross-router resource linkage makes this inconsistency more visible.

### Filesystem models and routes

Upstream changes all public filesystem operations to POST and introduces request-body models for operations that previously used query parameters:

- `PostFileRequest`
- `PostChecksumRequest`
- `PostStatRequest`
- `PostLsRequest`
- `PostHeadRequest`
- `PostTailRequest`
- `PostViewRequest`
- `PostRmRequest`
- `PostDownloadRequest`

This does not require fs-facade to change its internal HTTP verbs. The S3DF task adapter is the protocol boundary and may continue translating an upstream POST task into fs-facade GET, PUT, or DELETE operations.

Required S3DF changes:

1. Keep upstream request models and route methods.
2. Verify every `TaskCommand.args` shape against `_submit_to_fs_facade()`. The current upstream payload names are compatible for `file`, `stat`, `ls`, `head`, `tail`, `view`, `checksum`, `rm`, `download`, and upload.
3. Preserve request-model handling for chmod, chown, mkdir, symlink, compress, extract, move, and copy.
4. Restore authnz header context removed by upstream, including per-request reset, so headers cannot leak between requests.
5. Translate compression URNs to fs-facade's short compression values.
6. Add tests proving public POST request bodies become the existing fs-facade calls.
7. Retain direct `S3DFFilesystemAdapter` only if it is used outside the task-backed router; otherwise document its role. It still must import and instantiate cleanly because it can be configured through `IRI_API_ADAPTER_filesystem`.

### Task models

No structural task model change was found. The impact is in transport and context:

1. Preserve the shared IRI-task-ID to fs-facade-task-ID mapping behavior for compatibility.
2. Restore auth header forwarding after merging `request_context.py`.
3. Keep internal fs-facade verbs independent from public IRI POST methods.
4. Normalize enum/URN values before serializing request models to fs-facade.
5. Add concurrency and multi-worker risk documentation: the class-level task map is process-local and loses mappings on restart. This is pre-existing, but upstream idempotency and production multi-worker support make the limitation operationally important.

### New storage models and adapter

Upstream adds `/storage` with two authenticated methods:

```python
get_locations(resource, user, logicalpath, project, allocation, intent)
get_access_endpoints(resource, user, protocol, endpoint_id)
```

New model groups include:

- `LogicalName`: home, scratch, project, campaign, archive, shared, temporary.
- `StorageIntent`: read, write, staging, long-term-storage.
- `StorageInstance`: path, filesystem, performance tier, purge policy, shared flag, and access permissions.
- `AccessEndpoint`: Globus, XRootD, or S3 connection data and capabilities.

Decision required before release:

| Option | Impact |
| --- | --- |
| Implement `S3DFStorageAdapter` | Recommended if v2 storage is in scope. Resolve S3DF home/project/scratch paths using authenticated POSIX identity and CoAct project membership; expose approved Globus endpoints from deployment configuration. Add `IRI_API_ADAPTER_storage` wiring and exports. |
| Leave storage hidden | Do not set `IRI_API_ADAPTER_storage`. Confirm `IRI_SHOW_MISSING_ROUTES` is false in production. Document that the upstream optional/in-development storage API is intentionally unavailable at S3DF. |

Do not configure `IRI_API_ADAPTER_storage` to a class that lacks both abstract methods; application import will fail.

### Facility models

No upstream facility model or adapter signature change was found. Required integration work is limited but important:

1. Replace random facility/site UUIDs with stable configured IDs or deterministic UUIDs.
2. Ensure `Resource.site_id` values from the status adapter reference the returned `Site.id`.
3. Reapply local logo mounting only after preserving upstream `APP` and lifespan startup.

## Per-adapter impact summary

| S3DF component | Impact | Required change |
| --- | --- | --- |
| `S3DFAuthenticatedAdapter` | Medium | Auth interface is unchanged. Preserve Dex verification, but merge both project and auth header contexts. Remove diagnostic `print`; retain structured logging. Decide whether the currently disabled CoAct authorization check remains disabled. |
| `S3DFAccountAdapter` | Medium | Emit allocation URNs; map an optional stable project timestamp when CoAct provides one; add missing dedicated tests. |
| `SLACComputeAdapter` | High | Consume forwarded project, return models for idempotency, correct `job_spec`, retain Slurm JWT and historical lookup policies. |
| `S3DFFacilityAdapter` | Medium | Interface unchanged; make IDs stable and consistent with status resources. |
| `S3DFStatusAdapter` | High | Implement new abstract endpoint lookup; emit URN resource types and `supported_endpoints`. |
| `S3DFFilesystemAdapter` | High | Restore auth header context and translate compression URNs; verify role under task-backed routes. |
| `S3DFTaskAdapter` | High | Restore auth header context, preserve internal verb translation, and normalize v2 values for fs-facade. |
| New `S3DFStorageAdapter` | Decision/high | Implement locations and access endpoints or intentionally hide the router. |
| S3DF clients | Medium | Wire formats remain S3DF-specific. Add explicit translation at adapter boundaries rather than changing upstream models to match microservices. |

## Shared application and deployment changes

### Request context

The fork currently stores authnz-injected headers:

```text
x-auth-request-primary-gid
x-auth-request-gids
x-auth-request-uid
```

Upstream replaces that context with `X-IRI-Facility-Project`. The merged implementation must retain both. Use distinct `ContextVar` tokens and reset both in middleware `finally` blocks. Never use a mutable `{}` as a cross-request default if it can be mutated.

### Application startup

Upstream changes the FastAPI object from `app` to `APP`, adds a lifespan-managed idempotency store, and includes the storage router. Preserve these changes. Then reapply:

- S3DF auth header capture.
- Proxy-prefix URL behavior.
- SLAC logo static mounts, including the prefixed path required behind proxies.
- The deployment entry point expected by Gunicorn/Docker; update it if it still imports `app.main:app`.

### Dependencies

Resolve `pyproject.toml` by union, not by choosing one side. Verify at minimum:

- Upstream v2, validation, telemetry, and idempotency dependencies.
- The local slurmrestd wheel path dependency.
- PyJWT/JWKS/HTTP dependencies used by S3DF auth and clients.
- Python 3.13 compatibility.

Regenerate the lock file with the project's package manager and verify the bundled wheel remains available in container builds.

### Adapter environment wiring

Review and preserve these classes in development/deployment configuration:

```text
IRI_API_ADAPTER_account=app.s3df.account_adapter.S3DFAccountAdapter
IRI_API_ADAPTER_compute=app.s3df.compute_adapter.SLACComputeAdapter
IRI_API_ADAPTER_facility=app.s3df.facility_adapter.S3DFFacilityAdapter
IRI_API_ADAPTER_filesystem=app.s3df.filesystem_adapter.S3DFFilesystemAdapter
IRI_API_ADAPTER_status=app.s3df.status_adapter.S3DFStatusAdapter
IRI_API_ADAPTER_task=app.s3df.task_adapter.S3DFTaskAdapter
IRI_API_ADAPTER_storage=app.s3df.storage_adapter.S3DFStorageAdapter  # only if implemented
```

Compute and filesystem now depend directly on the status adapter for resource resolution. Treat `IRI_API_ADAPTER_status` as required whenever either domain is enabled.

## Validation gates

### Gate 1: clean integration state

```bash
git status --short
git diff --check origin/main...HEAD
git diff --name-status --diff-filter=D origin/main -- app/s3df
git diff --name-only --diff-filter=U
```

Expected result: no unresolved markers, no accidental loss of `app/s3df/`, and the PR #15 squash change represented exactly once. The source-branch commits `3d9eb9e` and `626a4ac` must not appear as additional replayed commits.

### Gate 2: dependency and import checks

```bash
uv sync
uv run pytest app/s3df/tests --collect-only -q
uv run python -c "from app.main import APP; print(APP.title)"
```

Instantiate every configured adapter, including storage if enabled. This catches missing abstract methods immediately.

### Gate 3: model contract tests

Add focused tests for:

- CoAct project mapping with optional, stable, timezone-aware `last_modified` when available.
- Capability and allocation unit URN serialization.
- Status resource URNs and `supported_endpoints`.
- `get_resources_for_endpoint()` filtering.
- Live and historical Slurm conversion using `job_spec`.
- Forwarded project selection reaching the Slurm `account` field.
- Rejection when both or neither project sources are present.
- Idempotent submit/update cache hit, body mismatch, in-flight conflict, and adapter exception cleanup.
- Compression URN to fs-facade short-name translation.
- Authnz header forwarding and per-request context reset.
- Every public filesystem POST body mapping to the expected fs-facade call.
- Storage location/endpoint filtering if storage is enabled.

### Gate 4: unit and generic API tests

```bash
uv run pytest app/s3df/tests -q
uv run pytest test -q
```

Classify known pre-existing failures before the rebase and do not hide new failures under that baseline. In particular, the disabled CoAct membership check and environment-sensitive filesystem tests need explicit expected status.

### Gate 5: OpenAPI v2 validation

Generate the OpenAPI document from the configured S3DF application and run the upstream API validation workflow. Confirm:

- URN examples and schemas are present.
- Filesystem operations are POST with request bodies.
- Compute and filesystem resource-list endpoints expose only supported resources.
- Hidden routes are intentional.
- Storage appears only when an S3DF storage adapter is configured.

### Gate 6: service-level smoke tests

In a non-production S3DF environment, verify:

1. Dex token authentication and user resolution.
2. CoAct project/capability/allocation retrieval.
3. Slurm submit with body account.
4. Slurm submit with forwarded project header.
5. Duplicate idempotent submit does not create a second job.
6. Live job status and historical single-job fallback.
7. Historical job list still returns the deliberate `501`.
8. Filesystem task submission forwards authnz headers and can be polled.
9. Status resources filter correctly for compute and filesystem endpoints.
10. Storage locations/access endpoints, if enabled.

## Release and rollback

Before updating the shared branch:

1. Push the integration branch without force-updating the existing branch.
2. Open a review comparing both `upstream/main...merge/upstream-v2-s3df` and `backup/origin-main-pre-upstream-v2-20260715...merge/upstream-v2-s3df`.
3. Record the chosen storage behavior and all intentionally preserved S3DF overrides.
4. Build and deploy an immutable test image.
5. Keep `backup/origin-main-pre-upstream-v2-20260715` until production smoke tests pass.

After approval, push the integration branch and merge it through the repository's normal protected-branch review process:

```bash
git push -u origin merge/upstream-v2-s3df
```

Rollback is a deployment rollback to the image built from `origin/main@5eab585`; the original tree is also retained at `backup/origin-main-pre-upstream-v2-20260715`. Do not delete the backup branch or pre-integration image until the v2 deployment has passed the service-level gates.

## Completion checklist

- [x] Current uncommitted `app/config.py` and `app/main.py` work preserved and manually reconciled.
- [x] Integration starts from refreshed `origin/main@5eab585`, not stale local `main` or the merged source branch.
- [x] PR #15 job-history changes are present exactly once; source commits `3d9eb9e` and `626a4ac` are not replayed.
- [x] Upstream v2 router and scalar contracts retained.
- [x] Dex, Slurm JWT, CoAct, status API, and fs-facade integrations retained.
- [x] Project/account header reaches Slurm submission and update.
- [x] Idempotent compute submit/update paths return serializable Pydantic models.
- [x] Canonical resource, allocation, and compression URNs are handled at model or adapter boundaries; optional project timestamps are not fabricated.
- [x] Status endpoint support implemented; environment validation remains.
- [x] Filesystem POST request models translated to the existing fs-facade protocol.
- [x] Authnz headers and facility-project context coexist with independent per-request reset.
- [x] Storage intentionally hidden in the S3DF image until an adapter is implemented.
- [x] Stable facility/site/resource identifiers established.
- [x] Dependencies, environment templates, Make targets, and container entry points reconciled for the first draft.
- [ ] S3DF tests, generic tests, OpenAPI validation, and service smoke tests pass.
- [ ] Backup branch and rollback image retained through deployment validation; the branch exists locally now.