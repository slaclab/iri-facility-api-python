# Upstream v2 Reconciliation Plan (October 2026)

This plan replaces the baselines in `upstream-v2-rebase-plan.md` and
`upstream-v2-merge-remaining-migrations.md` (July 2026). Those documents remain
useful for the migration analysis, but their references and the
`X-IRI-Facility-Project` design are out of date.

## 1. Situation on 2026-10-04

| Ref | Commit | What it is |
| --- | --- | --- |
| `origin/main` | `5eab585` | Last fork merge (PR #15, 2026-07-14). No longer what prod runs. |
| `origin/fix/fs-identity-forwarding` | `6b38ba0` | **Prod** (`ghcr.io/slaclab/iri-s3df:prod-09242026`). `main` + 3 commits. API v1. |
| `origin/merge/upstream-v2-s3df` | `894059f` | July v2 port: `main` merged with `upstream/main@7c06e2b`, plus the static storage adapter. |
| `upstream/main` | `5413e80` | doe-iri v2.0.0. 40 commits beyond the July merge base. |
| `upstream/v1.2.0`, tag `1.2.0` | `4c15928` | Upstream's v1 maintenance line. Not in `upstream/main`. |

Problems:

1. `main` no longer describes production; the deployed fixes exist only on a fix branch.
2. The v2 branch was missing the prod fixes (task ownership and TTL, fail-closed identity
   headers, live-job ownership and partition scoping, live `job_spec` mapping, CoAct
   allocation fixes, prefix-aware docs logo).
3. Upstream changed contracts after the July merge (section 4). Several changes merge
   without textual conflicts but break or silently change S3DF behaviour.

## 2. Strategy

**Continue with merges; do not rebase.** `merge/upstream-v2-s3df` is published and already
contains a merge from upstream. A rebase would rewrite that branch and replay about 70 fork
commits over about 100 upstream commits, repeating the generic conflicts. Simulated merges
(`git merge-tree`) showed small, ownership-separable conflict sets:

| Step | Conflicting files |
| --- | --- |
| v2 branch ← prod fixes | `app/config.py`, `app/s3df/compute_adapter.py`, `app/s3df/task_adapter.py` |
| v2 branch ← `upstream/main` | `README.md`, `app/config.py`, `app/main.py`, `local-template.env`, `pyproject.toml` |
| *(rejected)* prod ← `upstream/main` from scratch | 8 files, and it would redo the July port |

This supersedes the rebase-first default in the `s3df-upstream-rebase` skill for this fork:
upstream syncs are `git merge upstream/main` into the S3DF integration line, resolved by
file ownership (`app/routers/**` and `app/types/**` from upstream, `app/s3df/**` from S3DF).

## 3. Target branch model

```text
upstream/main ──merge──> main (S3DF v2 line, mirrors upstream/main)
                           ^
                           └── PR from the integration branch once validated

release/v1  (cut from 6b38ba0) ── S3DF v1 hotfixes; forward-merge into main
tags        s3df-v1-prod-09242026 -> 6b38ba0   (immutable record of the prod image)
            s3df-v2-<date>          -> each v2 deployment
```

1. Tag the prod commit and fast-forward `main` to `6b38ba0` so `main` is truthful again
   (fix/fs-identity-forwarding is strictly ahead of `main`; no merge commit is required).
2. Cut `release/v1` from `6b38ba0` for v1 hotfixes while v1 is still served.
3. Push the integration result as a fast-forward of `merge/upstream-v2-s3df` (no force push)
   and open a PR into `main`. After it merges, `main` is the v2 line, as upstream's is.
4. Hotfixes to v1 land on `release/v1` and are merged forward into `main`.
5. After the v1 deployment is retired, keep `release/v1` read-only or delete it.

Image tags must say which line built them. The v2 line builds `v2-MMDDYYYY`; only
`release/v1` builds `prod-MMDDYYYY`. Prod pulls reused tags with `imagePullPolicy: Always`,
so a v2 build that reused a `prod-*` tag would replace the running v1 image on the next
restart.

## 4. Upstream changes since July that S3DF must handle

| Upstream change | S3DF impact | Handling |
| --- | --- | --- |
| `X-IRI-Facility-Project` header and `get_iri_facility_project()` removed (fd4c009) | July compute adapter imported it; import failure | Removed in the merge commit. Account comes from `job_spec.attributes.account`, then `SLURM_DEFAULT_ACCOUNT`. Slurm associations remain the authority on which accounts a user may charge. |
| Globus introspection removed; `get_current_user_globus` dropped from the ABC; `get_user()` loses `globus_introspect` | Dead S3DF code paths | Remove Globus remnants from S3DF adapters. |
| AmSC Keycard auth (`app/amsc_auth.py`), off unless `AMSC_TOKEN_ENABLED=true`; `/account/whoami` | `/whoami` works through `get_user`; AmSC is a separate decision | Leave AmSC disabled (decision D5). |
| `app/demo_adapter.py` moved to the `examples/demo-adapter` submodule; most `test/` files moved with it | July docs/env referenced `app.demo_adapter.*` | References updated. S3DF images do not need the submodule. |
| Idempotency store is opt-in; unset means `Idempotency-Key` requests get 501 | v2 clients that send the header would fail | Ship an S3DF store (decision D6). |
| HAL `_links` on every `NamedObject`; job and filesystem operation affordances | Compute affordances require `resource_type` under `urn:doe-iri:resource:compute:system` | Re-type the registry (decision D8). |
| Resource-definition subtypes (`compute:system`, `storage:filesystem`, ...) and `related_resource_ids` | Registry used the parent types | Re-type the registry (decision D8). |
| `IRIBaseModel.attributes` (optional, dropped when null) | None for current dict payloads | Covered by existing model validation tests. |
| Status `get_resources(capability: list[str])`; incidents/events return `[]` instead of 404 | S3DF already passes `capability` through to `Resource.find` and returns `[]` | Align the annotation. |
| `IRI_SHOW_MISSING_ROUTES=true` now fails startup for any unset adapter | The prod Dockerfile sets it `true` | v2 Dockerfile sets it `false`; all adapters are configured. |
| `_lifespan` calls `close()` on the store even when it is `None` | Shutdown `AttributeError` when no store is configured | Avoided by configuring a store; report upstream. |

## 5. Work items

### Phase 1: integration (local branch, done)

- [x] Baseline tests on prod (`6b38ba0`): S3DF 89 pass / 3 known failures; `test/test_docs_logo.py` 4 pass.
- [x] Baseline on July branch (`894059f`): S3DF 56 pass / 4 failures (3 known + a URN `is` comparison).
- [x] Merge prod fixes into the July branch (`4125fc0`).
- [x] Merge `upstream/main@5413e80` (`2019e78`); remove the project-header dependency.
- [x] All seven S3DF adapters instantiate; OpenAPI builds with 47 `/api/v2` paths.
- [x] S3DF 102 pass / the same 4 failures; `test/test_docs_logo.py` 4 pass.

### Phase 2: v2 build-out (integration branch)

- [x] Image tag for the v2 line (`v2-MMDDYYYY`), never `prod-*`; `dev-s3df` no longer sets
      `IRI_SHOW_MISSING_ROUTES=true`, which now fails startup (`4083622`).
- [x] Restore upstream's copies of `app/routers/compute/compute.py` and
      `app/routers/status/status.py` (whitespace-only divergence) (`a6e026a`).
- [x] Account: upstream's required `Project.last_modified`, mapped from the CoAct repo
      ObjectId (decision D7) (`2d6d3df`).
- [x] Status: `compute:system` / `storage:filesystem` subtypes, `capability: list[str]`,
      HAL affordance tests, URN comparison test fixed (`c160d17`).
- [x] Storage: accept `storage:*` subtypes for `sdfhome`; the old exact-type check would have
      returned 501 for every `/storage` request after re-typing (`c160d17`).
- [x] Idempotency: `app.s3df.idempotency.InMemoryIdempotencyStore`, configured in the image,
      `dev-s3df`, and the env template (`58bfb1a`).
- [x] Auth: Globus remnants removed (`80b1113`). The `Authenticated user` print is kept
      because the troubleshooting runbooks key on it.
- [x] Docs: this plan; July docs marked superseded. `README.md` stays identical to upstream.

`app/routers/**` and `app/types/**` are identical to `upstream/main`. Shared files differ only in
S3DF wiring: `app/main.py` (authnz-header context, logo mounts, lowercase `app` alias),
`app/config.py` (title, S3DF URL root, logo URL), `app/request_context.py` (authnz-header
context), and `pyproject.toml` (CoAct/Slurm client dependencies).

Not yet done:

- [ ] Decide D3 and prepare the `iri-deploy` overlay for a separate v2 deployment.
- [ ] Extend storage discovery beyond `sdfhome` (D9).
- [ ] Optional: `related_resource_ids` (for example, which filesystems are mounted on which
      partitions) once operators confirm the mount topology.
- [ ] Report upstream: `_lifespan` calls `close()` on a `None` idempotency store at shutdown.

### Phase 3: validation before review

- [x] `uv run python -m pytest app/s3df/tests test -q`: 124 pass; only the 3 known failures
      (2 for the commented-out CoAct membership check, 1 filesystem download).
- [x] Operations compared with the official
      `iri-facility-api-docs/specification-v2/openapi/all_spec_v2.yaml`: all 47 official
      operations present with matching `operationId`s, plus upstream's `/account/whoami`, which
      the published spec does not list yet. Remaining schema differences (`_links` marked
      required) come from upstream's routers, which S3DF uses unchanged.
- [x] End-to-end idempotency through the app (lifespan store → v2 router → S3DF compute adapter
      with stubbed Slurm): miss, hit with the same job id and no second submit, 422 on a changed
      body; `job_spec.attributes.account` reaches the Slurm request.
- [x] Image builds; with deployment-supplied `DEX_AUDIENCE` it starts with all seven adapters and
      the S3DF store, serves `/api/v2` (and 404 for `/api/v1`), and shuts down cleanly.
- [ ] Schemathesis (`api-validation.yml`) against a v2 dev deployment; it needs live backends.
- [ ] Dev deployment smoke tests (section 8).

### Phase 4: publish and roll out (needs maintainer approval)

- [ ] Tag prod, fast-forward `main`, cut `release/v1` (section 3).
- [ ] Push the integration branch as a fast-forward of `merge/upstream-v2-s3df`; open the PR.
- [ ] Deploy v2 beside v1 (decision D3); keep the v1 deployment and image until v2 passes.
      The v2 deployment needs the same config and secrets as v1 (`DEX_AUDIENCE`, `SLURM_JWT`,
      `SLURM_REST_URL`, CoAct credentials, `FS_FACADE_URL`, `S3DF_STATUS_API_URL`); `API_URL`
      now defaults to `api/v2`, so do not copy an `API_URL=api/v1` override from the v1 overlay.
      fs-facade must accept calls from the new deployment (NetworkPolicy).

## 6. Decisions

Defaults are what the integration branch implements unless a maintainer decides otherwise.

| # | Decision | Default |
| --- | --- | --- |
| D1 | Sync strategy | Merge (section 2). |
| D2 | Branch model | Section 3; `main` becomes the v2 line and mirrors upstream. |
| D3 | v1/v2 deployment | Run both: the existing `iri-s3df` serves `/api/v1`; a new deployment serves `/api/v2`. Traefik ForwardAuth already covers `/api/v{1,2}/...`. Retire v1 when clients have moved. Needs `iri-deploy` changes. |
| D4 | Compute account source | `job_spec.attributes.account`, else `SLURM_DEFAULT_ACCOUNT`; Slurm enforces associations. |
| D5 | AmSC Keycard auth | Disabled. Enabling requires an AmSC-project → S3DF-user mapping file and authnz uid/gid headers for the mapped user. Separate design. |
| D6 | Idempotency store | S3DF in-process store. Correct for one replica running one process (`fastapi run`). Cached results are lost on restart. Use a shared store (Redis) before scaling out. |
| D7 | `Project.last_modified` | Creation time embedded in the CoAct repo `Id` (a MongoDB ObjectId): stable and needs no extra query. Projects have no `modified_since` filter, so the value is informational. |
| D8 | Resource types | Slurm partitions → `urn:doe-iri:resource:compute:system` (unlocks job affordances); Weka filesystems → `urn:doe-iri:resource:storage:filesystem`. Partitions stay separate resources so existing `resource_id`s keep working. |
| D9 | Storage scope | July static `sdfhome` adapter; other locations and Globus endpoints follow `design-docs/s3df-storage-adapter.md`. |

## 7. Preserved S3DF behaviour (regression checklist)

- Dex JWKS verification; CoAct user, project, and allocation mapping (`Id`, not `_id`).
- Per-user HS256 Slurm JWT (`sun`), `X-SLURM-USER-TOKEN` / `X-SLURM-USER-NAME`.
- Live jobs filtered by authnz uid (user_name fallback) and by partition; hidden without identity.
- Historical single-job lookup through slurmdbd with an ownership check; historical listing stays 501.
- Task records owned by the submitter, expiring after `FS_TASK_TTL`; other users' tasks behave as missing.
- fs-facade calls fail closed (401/400) without valid uid/primary-gid; identity forwarded on submit,
  poll, and delete; primary gid first in `x-auth-request-gids`.
- Compression URNs translated to fs-facade short names.
- Root and prefix-aware docs logo routes; lowercase `app` alias for existing entry points.

## 8. Smoke tests for the first v2 deployment

1. `GET /api/v2/facility`, `/status/resources` (types, `_links`, job affordances on partitions).
2. `GET /api/v2/account/whoami`, `/projects` (with `last_modified`), allocations.
3. Submit with `job_spec.attributes.account`; resubmit with the same `Idempotency-Key` and get the
   same job id; same key with a different body returns 422.
4. Live job status, historical single-job fallback, historical list 501.
5. Filesystem task submit, poll, delete with a second user proving isolation (404).
6. `GET /api/v2/storage/locations/sdfhome`.

## 9. Housekeeping (needs approval; affects shared state)

- Remote branches fully contained in `origin/main`: `feat/facility-adapter`,
  `feat/mock-static-endpoints`, `feat/resource-model`, `feat/s3df-auth`,
  `feat/services-integration`, `feat/status-adapter`, `feat/user-lookup-integration`,
  `passthru-custom-attr`, `sync/upstream-merge`.
- Superseded: `feat/job-history-query` (squash-merged as `5eab585`).
- Unmerged work to triage before deleting: `feat/slurm-integration` (flood-submission test
  script, 2 commits), `other-supported` (`b65ad7e`, 422 for custom attributes that do not fit the
  Slurm model).
- Stashes: `stash@{0}` (WIP on `merge/upstream-v2-s3df`) is an earlier draft of the fs-identity and
  logo fixes that landed in `4e9b4c6`/`1c6e862`; `stash@{1}` is the July pre-merge work that the July
  plan marks as reconciled. Both can be dropped after review.

## 10. Ongoing upstream sync

1. `git fetch upstream origin`, then simulate:
   `git merge-tree --write-tree --name-only --messages main upstream/main`.
2. Merge on a short-lived branch; resolve by ownership; never edit `app/routers/**` or
   `app/types/**` to fit S3DF payloads, and translate at the adapter boundary instead.
3. Grep for removed upstream symbols before committing the merge, then instantiate every
   configured adapter and run both test suites.
4. Upstream branches to watch: `amsc_auth` (DNSBL revocation, `allowed_sub`) and
   `fix_ext_url` (problem URLs from forwarded host and prefix).
