# S3DF Storage Adapter — Technical Design

## Initial implementation decision

**Implemented: static `sdfhome` discovery only.** This decision supersedes the
dynamic discovery proposal retained later in this document as possible future work.

- `GET /storage/locations/sdfhome` returns
  `/sdf/home/{username[0]}/{username}` for the authenticated username.
- The response is declared static metadata. The adapter does not call CoAct,
  `fs-facade`, the filesystem, or an ACL/effective-access service.
- Project- and allocation-scoped discovery return `501 Not Implemented`.
- Resources other than `sdfhome` return `501 Not Implemented`.
- Remote access endpoints for `sdfhome` return an empty list. No protocol is
  advertised yet.

The initial implementation therefore has constant work per request and does not
enumerate projects, allocations, directories, or ACLs. The broader design below is
deferred and must not be treated as deployed behavior.

## Summary

This document defines the S3DF implementation of the IRI v2 `/storage` API in
`iri-facility-api-python`.

The initial adapter discovers authorized paths for:

- `sdfhome`
- `sdfdata`
- `sdfscratch`

It excludes `sdfk8s`, compute resources, and archive storage. It does not perform
filesystem operations, provision storage, transfer data, or report storage usage.

The design keeps four authorities separate:

```text
/status registry     -> resource identity and classification
Coact                -> project membership and storage allocation metadata
fs-facade-service    -> effective POSIX/ACL access on mounted /sdf storage
IRI runtime config   -> S3DF path policy and approved remote endpoint metadata
```

The `/storage/access-endpoints` implementation is configuration-driven, but its
initial registry is empty. No Globus, XRootD, S3, or other protocol is advertised
until S3DF infrastructure owners verify the deployed service, authentication
method, exposed paths, and capabilities.

## Status

**Partially implemented.** The static `sdfhome` scope described above is enabled.
The dynamic `sdfdata`/`sdfscratch`, project/allocation, effective-access, and remote
endpoint portions of this proposal remain deferred.

## Scope

### In scope

- Resolve the authenticated user's S3DF home path.
- Discover project and scratch paths from user-scoped Coact storage allocations.
- Validate candidate paths against resource-specific S3DF roots.
- Determine effective read, write, and traverse access through a filesystem-side
  check executed under the user's numeric POSIX identity.
- Implement the generic `logicalpath`, `project`, `allocation`, and `intent`
  filters.
- Define a typed remote-access endpoint registry.
- Return an empty endpoint list until actual S3DF protocols are approved.
- Define required changes in `iri-facility-api-python`, `fs-facade-service`, and
  `iri-deploy`, with a reason for each cross-repository change.

### Non-goals

- File operations such as list, stat, upload, download, copy, or chmod. Those
  belong to `/filesystem`.
- Storage creation, allocation requests, quota changes, or directory provisioning.
- Storage capacity and usage reporting. Coact allocation usage belongs to the
  account/allocation domain unless the generic IRI contract is extended.
- Credential minting, presigned URLs, transfer initiation, or secret distribution.
- Compute-partition-specific paths. S3DF compute resources share the relevant
  filesystems and are not `/storage` resource contexts.
- `sdfk8s` persistent-volume discovery.
- Archive/HPSS discovery.
- Claiming an enforced scratch purge period before the current policy is confirmed.

## Generic IRI contract

The generic implementation already exists under `app/routers/storage/`.

### Routes

```text
GET /storage/locations/{resource_id}
GET /storage/access-endpoints/{resource_id}
```

Both routes:

- require an authenticated `User`;
- resolve `resource_id` through the configured `/status` adapter;
- return `404 Resource not found` when the status resource is unknown;
- reject undeclared query parameters.

### Adapter methods

```python
async def get_locations(
    resource: Resource,
    user: User,
    logicalpath: LogicalName | None,
    project: str | None,
    allocation: str | None,
    intent: StorageIntent | None,
) -> list[StorageInstance]

async def get_access_endpoints(
    resource: Resource,
    user: User,
    protocol: AccessProtocol | None,
    endpoint_id: str | None,
) -> list[AccessEndpoint]
```

### Returned location data

`StorageInstance` contains:

| Field | S3DF source |
| --- | --- |
| `logical_name` | S3DF resource/allocation policy |
| `path` | Home policy or Coact allocation `rootfolder` |
| `filesystem` | S3DF runtime policy |
| `performance_tier` | S3DF runtime policy |
| `purge_policy_days` | S3DF runtime policy after owner confirmation |
| `shared` | Logical tier/allocation policy |
| `access.read` | fs-facade effective-access result |
| `access.write` | fs-facade effective-access result |
| `access.execute` | fs-facade effective traverse result |

The adapter must return exact model field names. It must not add Coact quota or
usage fields that the generic `StorageInstance` model does not declare.

## Verified facts

### S3DF resource vocabulary

`app/s3df/status_registry.py`, mirrored from
`s3df-status-api/resources.yaml`, defines:

| Resource | Type | Description |
| --- | --- | --- |
| `sdfhome` | storage | Home directories under `/sdf/home` |
| `sdfdata` | storage | Project/group data |
| `sdfk8s` | storage | Kubernetes persistent volumes |
| `sdfscratch` | storage | Scratch storage under `/sdf/scratch` |

The initial adapter allowlist is narrower than the status resource list:
`sdfhome`, `sdfdata`, and `sdfscratch`.

### S3DF path documentation

The checked-in S3DF documentation describes:

```text
/sdf/home/<first-letter>/<username>
/sdf/data/<facility>/...
/sdf/scratch/<facility>/...
```

It also states that scratch is intended as best-effort three-month retention, but
automatic purge was not active as of September 2025. Therefore
`purge_policy_days` remains unset until the current enforced policy is confirmed.

### Coact storage model

The Coact `RepoStorageAllocation` model exposes:

```text
_id
repoid
storagename
purpose
rootfolder
start
end
gigabytes
inodes
```

The IRI Coact client already has:

- a user-impersonated repo query;
- `get_repo_storage_allocations(repo_id, username)`;
- current allocation filtering performed by Coact;
- an authoritative-looking `rootfolder` field.

The current IRI client catches broad exceptions and returns `[]`. That behavior is
not safe for `/storage`: an authorization-sensitive backend failure must not look
like "the user has no allocations."

### Filesystem-side identity and access

`fs-facade-service`:

- runs with `/sdf` mounted;
- accepts numeric UID and GID headers;
- queues operations with that identity;
- has a privileged worker that calls `setgroups`, `setresgid`, and `setresuid`
  before performing filesystem operations;
- validates resolved paths against configured allowed roots.

It does not currently expose a purpose-built effective-access operation.

`stat()` metadata is insufficient for this adapter because extended ACLs, ACL
masks, named users/groups, and parent-directory traversal affect effective access.
The kernel must evaluate access under the intended identity.

### Deployment state

- The API image and local environment template configure
  `IRI_API_ADAPTER_storage` for the static adapter.
- External deployment routing must expose and protect the v2 storage route; that
  configuration is outside this repository and must be verified in dev.
- Coact, fs-facade, and S3DF status service URLs may already be configured, but
  static `sdfhome` discovery does not use them.
- The API currently captures `x-auth-request-uid`,
  `x-auth-request-primary-gid`, and `x-auth-request-gids`; the static adapter does
  not consume them.
- The checked-in ForwardAuth middleware lists
  `X-Auth-Request-GidNumbers` rather than the UID/GID header names consumed by the
  API and fs-facade. Resolve that mismatch before enabling dynamic effective-access
  discovery.

The identity-header contract is therefore not ready for production use and must be
reconciled before enabling the adapter.

## Proposed architecture

### Location discovery flow

```text
Client
  |
  | GET /storage/locations/{resource_id}
  v
IRI storage router
  |
  | authenticate bearer token
  | resolve status resource
  v
S3DFStorageAdapter
  |
  | validate resource type + explicit S3DF allowlist
  | validate filters
  |
  +---- sdfhome ----------------------------+
  |      configured home path policy        |
  |                                         |
  +---- sdfdata / sdfscratch ---------------+
         Coact user-impersonated repos       |
         + current storage allocations       |
         + allocation rootfolder             |
                                             v
                                candidate path policy check
                                             |
                                             v
                           fs-facade effective-access check
                           under forwarded UID and GID set
                                             |
                                             v
                                  StorageInstance mapping
                                             |
                                             v
                                         Client
```

### Access endpoint discovery flow

```text
Client
  |
  | GET /storage/access-endpoints/{resource_id}
  v
S3DFStorageAdapter
  |
  | validate resource
  | load typed endpoint registry
  | filter by resource, protocol, endpoint id
  v
[] until an endpoint is explicitly configured and approved
```

### Adapter structure

```text
app/s3df/storage_adapter.py
  S3DFStorageAdapter(
      S3DFAuthenticatedAdapter,
      storage.facility_adapter.FacilityAdapter,
  )

app/s3df/storage_policy.py
  resource allowlist
  root constraints
  home path policy
  Coact allocation mapping
  location metadata
  endpoint registry parsing

app/s3df/clients/coact.py
  failure-preserving storage allocation queries

app/s3df/clients/fs_facade.py
  effective-access request/response support
```

Site-specific path and endpoint policy must not be added to the generic
`app/routers/storage/` package.

## Resource policy

### Initial mapping

| Resource | Logical name | Candidate path authority | Coact required | Initial metadata |
| --- | --- | --- | --- | --- |
| `sdfhome` | `home` | Configured home template | No | `shared=false`, no purge claim |
| `sdfdata` | `project` | Coact allocation `rootfolder` | Yes | `shared=true`, no purge claim |
| `sdfdata` | `shared` | Deferred pending `purpose` semantics | Yes | Not returned initially |
| `sdfscratch` | `scratch` | Coact allocation `rootfolder` | Yes where allocation-backed | `shared` derived from allocation policy |
| `sdfk8s` | none | Excluded | No | Rejected |
| compute resources | none | Invalid storage context | No | Rejected |
| archive | none | Deferred | No | Not returned |

### Resource validation

The adapter accepts a resource only when:

```text
resource.resource_type == storage
AND resource.id in {"sdfhome", "sdfdata", "sdfscratch"}
```

Storage type alone is insufficient because `sdfk8s` is not user-facing storage
discovery. Compute resources such as `ada` and `ampere` are rejected even though
jobs can access shared S3DF filesystems.

Recommended error:

```text
404 Resource not available for storage discovery
```

Using a non-enumerating `404` avoids exposing distinctions between internal
resources and unsupported user-facing resources.

## Candidate path resolution

### Home

The home candidate is produced from configured policy:

```text
/sdf/home/{first}/{username}
```

Requirements:

- `username` comes from the authenticated identity, never a query parameter.
- Validate the username against the S3DF canonical username rules before
  interpolation.
- Derive `{first}` only after normalization is defined.
- Resolve and validate the result under the configured `sdfhome` root.
- Do not call Coact solely to discover a user's own home path.
- Do not return the path unless fs-facade confirms it exists and reports usable
  traverse/read or write access according to the requested intent.

The exact username normalization and symlink behavior remain review gates.

### Project data

For `sdfdata`:

1. Query Coact as the authenticated user for repos visible to that user.
2. Apply the `project` filter to the user-scoped repo set when present.
3. Query current storage allocations for the selected repos.
4. Apply the `allocation` filter when present.
5. Select allocations mapped to `sdfdata` and logical name `project`.
6. Use `rootfolder` as the candidate path.
7. Validate that the canonical path remains under the configured `sdfdata` roots.
8. Ask fs-facade for effective access.
9. Return only accessible candidates.

The adapter must never generate a path by appending a caller-provided project or
allocation string to `/sdf/data`.

### Scratch

For `sdfscratch`, use the same Coact allocation flow when Coact has an
authoritative scratch allocation and `rootfolder`.

Do not infer:

```text
/sdf/scratch/{project}
/sdf/scratch/{facility}
/sdf/scratch/{username}
```

from a repo name, facility name, or authenticated username without an approved
policy. If Coact cannot authoritatively supply scratch roots, `sdfscratch`
discovery remains disabled until another owner supplies that mapping.

### Canonical path validation

Every candidate must satisfy all of the following:

1. It is absolute.
2. It contains no NUL byte.
3. Its configured resource mapping is valid.
4. Its canonical path remains under an allowed root for that resource.
5. Its canonicalization behavior matches the approved symlink policy.
6. Coact-derived paths are not modified using caller-provided strings.

Root checks must be path-component-aware; string prefix checks such as
`path.startswith("/sdf/data")` are insufficient because `/sdf/database` would
also match.

## Identity and authorization

### Trust model

```text
Dex bearer token
  -> authenticated application username

ForwardAuth response headers
  -> trusted numeric UID, primary GID, supplementary GIDs

Coact user impersonation
  -> project/repo visibility and allocation entitlement

fs-facade privilege drop + kernel access check
  -> effective path access
```

No single source replaces the others:

- Dex does not prove POSIX identity or project storage entitlement.
- Coact entitlement does not prove current filesystem access.
- A matching GID does not provide a stable public project-to-path mapping.
- A successful discovery response does not authorize later `/filesystem`
  operations; every operation must enforce access again.

### Required identity-header contract

Before rollout, infrastructure owners must document:

- the service that produces numeric UID/GIDs;
- exact header names;
- the supplementary-group delimiter;
- whether the primary GID is repeated in supplementary groups;
- whether all values are canonical decimal integers;
- how spoofed client copies are removed;
- whether the authenticated username-to-UID binding is guaranteed;
- behavior for users without POSIX identity data.

The API and fs-facade must use one shared contract. Missing or malformed identity
must fail closed in deployed S3DF mode.

### Effective-access contract

Add a read-only fs-facade operation conceptually equivalent to:

```json
{
  "path": "/sdf/data/example"
}
```

Response:

```json
{
  "exists": true,
  "read": true,
  "write": false,
  "execute": true
}
```

The operation:

- uses the same forwarded UID/GID context as filesystem operations;
- executes after the worker drops privileges;
- validates the candidate against allowed roots;
- asks the operating system for effective read/write/execute access;
- does not parse ACL text in the IRI API;
- does not mutate the filesystem;
- distinguishes malformed identity, path-policy rejection, missing path,
  permission denial, and internal failure.

For directories, `execute` means traversal. A location is not useful when the user
cannot traverse the path.

This fs-facade change has a narrow purpose: Coact and the API cannot evaluate
mounted-filesystem ACLs reliably. It is not a general expansion of `/storage` into
filesystem operations.

## Query filter semantics

### Project and allocation identifiers

Proposed public meanings:

| Query parameter | Meaning |
| --- | --- |
| `project` | Coact repo/project ID |
| `allocation` | Coact storage allocation ID |

These meanings require confirmation by Coact and IRI API owners.

Rules:

- Supplying both `project` and `allocation` returns `400`.
- Neither value is interpolated into a filesystem path.
- `project` is matched only against the user-scoped Coact repo result.
- `allocation` is matched only against allocations reachable through the user's
  scoped repos.
- Unauthorized and unknown values use the same non-enumerating response.

Recommended response for an explicitly requested but unavailable identifier:

```text
404 No storage location found
```

### Logical-name filter

| Resource | Accepted initial logical name |
| --- | --- |
| `sdfhome` | `home` |
| `sdfdata` | `project` |
| `sdfscratch` | `scratch` |

Other logical names produce no result. The generic router already turns an empty
result with an explicit `logicalpath` into `404`.

### Intent filter

| Intent | Required result |
| --- | --- |
| unset | Return candidates with usable traverse access and report actual permissions |
| `read` | `read=true` and `execute=true` |
| `write` | `write=true` and `execute=true` |
| `staging` | Non-archive candidate with usable access; archive is absent initially |
| `long-term-storage` | Empty result until archive is modeled |

The adapter reports actual access in every returned `StorageInstance`; intent only
filters candidates.

### No-filter behavior

When neither `project` nor `allocation` is supplied:

- `sdfhome` returns at most one location;
- `sdfdata` and `sdfscratch` return all accessible, current, user-entitled
  allocations up to a configured result limit;
- results are deduplicated by canonical path and logical name;
- results are sorted deterministically by logical name, path, and allocation ID.

The API should fail explicitly rather than silently truncate when Coact returns
more than the configured safety limit. The final limit is an operational setting.

## Decision tables

### Resource eligibility

| Resource input | Result |
| --- | --- |
| Unknown status resource | Existing router `404` |
| Compute resource | Adapter `404` |
| `sdfk8s` | Adapter `404` |
| `sdfhome` | Continue |
| `sdfdata` | Continue |
| `sdfscratch` with approved allocation mapping | Continue |
| `sdfscratch` without approved mapping | Empty or `501` during development; keep route disabled for release |

### Home discovery

| Condition | Result |
| --- | --- |
| Missing/malformed numeric identity | Authentication/identity error |
| Invalid username for home policy | `400` or identity configuration error |
| Canonical path escapes `sdfhome` root | Service configuration error; do not return path |
| Path missing | Empty result |
| No traverse access | Empty result |
| `intent=read`, no read access | Empty result |
| `intent=write`, no write access | Empty result |
| Accessible | One `StorageInstance` |

### Project/allocation discovery

| Condition | Result |
| --- | --- |
| Coact unavailable or GraphQL error | `502`/`503`, not `[]` |
| Project not in user-scoped repos | Non-enumerating `404` |
| Allocation not reachable through user's repos | Non-enumerating `404` |
| Allocation expired/not current | `404` |
| Missing or malformed `rootfolder` | Upstream data error; omit only if policy explicitly permits partial results |
| Root outside configured resource roots | Upstream/policy error; never return path |
| Path missing | Empty result for unfiltered discovery; `404` for explicit filter |
| Coact entitlement but filesystem denies access | Empty result for unfiltered discovery; `404` for explicit filter |
| Accessible | `StorageInstance` |

### Partial failure policy

Recommended default:

- Coact request failure fails the whole request.
- Identity or fs-facade transport failure fails the whole request.
- Malformed allocation records fail the whole request and identify the allocation
  only in server logs.
- A valid but inaccessible candidate is omitted.

Returning partial results after an upstream failure can mislead clients into
believing the response is complete and is not the initial design.

## Health and availability

`/status` owns resource health. The `/storage` route already resolves the status
resource before calling the adapter.

Recommended behavior:

- Continue location discovery when the resource status is `down`, `degraded`, or
  `unknown`.
- Do not add a second status-service call inside `S3DFStorageAdapter`.
- Surface fs-facade failure independently because the adapter cannot establish
  effective access without it.

This avoids coupling discovery policy to monitoring availability while preserving
health visibility through `/status`. The behavior remains an open design-review
decision.

## Access endpoints

### Purpose

An `AccessEndpoint` tells a client how an already-authorized storage resource can
be reached through a remote protocol. It does not initiate a transfer or issue
credentials.

Examples supported by the generic model include Globus, XRootD, and S3, but model
support does not prove that S3DF operates any of them for these resources.

### Initial behavior

The initial endpoint registry is empty:

```text
GET /storage/access-endpoints/sdfhome    -> []
GET /storage/access-endpoints/sdfdata    -> []
GET /storage/access-endpoints/sdfscratch -> []
```

Protocol and endpoint-ID filters are applied to the configured registry. With an
empty registry they also return `[]`.

### Configuration shape

Illustrative configuration:

```json
{
  "endpoints": [
    {
      "id": "stable-public-id",
      "resource_ids": ["sdfdata"],
      "protocol": "xrootd",
      "display_name": "Approved S3DF endpoint",
      "auth_type": "oidc",
      "capabilities": ["list", "read", "streaming"],
      "endpoint": "root://approved.example/"
    }
  ]
}
```

Configuration validation must enforce protocol-specific fields and reject:

- duplicate endpoint IDs;
- unknown resource IDs;
- unsupported capability values;
- missing protocol-specific connection fields;
- credentials or secret-looking fields;
- endpoint roots that have not been approved for the resource.

### Infrastructure inventory template

Before enabling an endpoint, record:

| Field | Required answer |
| --- | --- |
| Service owner | Team and operational contact |
| Protocol | Actual deployed protocol |
| Stable endpoint ID | Identifier safe for API clients |
| Address | URI, host, endpoint URL, collection ID, or equivalent |
| Exposed resources | `sdfhome`, `sdfdata`, `sdfscratch` |
| Exposed roots | Exact path mapping |
| Authentication | OIDC, SSH, service-specific identity, etc. |
| Authorization | How user/project access is enforced |
| Capabilities | list/read/write/transfer/streaming |
| Network scope | Internal, SLAC, ESnet, public |
| User path mapping | How the authenticated identity maps to a remote path |
| Sensitive metadata | Whether discovery requires entitlement |
| Credential boundary | Confirmation that API responses contain no secrets |
| Health owner | Monitoring and incident response |

## Configuration

Use typed S3DF settings or a dedicated JSON/YAML policy file. Proposed settings:

| Setting | Purpose | Safe default |
| --- | --- | --- |
| `S3DF_STORAGE_ALLOWED_RESOURCES` | Explicit resource allowlist | `sdfhome,sdfdata,sdfscratch` |
| `S3DF_STORAGE_HOME_ROOT` | Canonical home root | `/sdf/home` |
| `S3DF_STORAGE_HOME_TEMPLATE` | Home path template | `/sdf/home/{first}/{username}` |
| `S3DF_STORAGE_DATA_ROOTS` | Allowed project data roots | `/sdf/data` |
| `S3DF_STORAGE_SCRATCH_ROOTS` | Allowed scratch roots | `/sdf/scratch` |
| `S3DF_STORAGE_RESULT_LIMIT` | Maximum candidate/allocation count | Explicit operational value |
| `S3DF_STORAGE_ENDPOINTS_FILE` | Typed endpoint registry | Unset/empty |
| `S3DF_STORAGE_REQUIRE_POSIX_IDENTITY` | Fail closed without UID/GIDs | `true` in deployed S3DF |

Filesystem labels, performance tiers, sharing policy, retention, and Coact
`storagename`/`purpose` mappings should live in the same validated policy rather
than scattered conditionals.

Configuration errors must fail adapter startup or route activation. They must not
silently remove a resource or broaden an allowed root.

## Error and privacy semantics

### HTTP mapping

| Condition | Recommended status |
| --- | --- |
| Invalid combination of `project` and `allocation` | `400` |
| Unsupported/non-user-facing resource | `404` |
| Explicit logical name has no result | Existing router `404` |
| Unknown or unauthorized explicit project/allocation | `404` |
| Missing trusted POSIX identity | `401` or `403`, aligned with auth middleware |
| Malformed trusted identity | `400` or `502`, depending on producer contract |
| Coact transport/GraphQL failure | `502`/`503` |
| fs-facade transport failure | `502` |
| fs-facade timeout | `504` |
| Invalid site policy or escaped path | `500`/`502`, with sanitized client detail |

### Information that must not leak

- repos the user cannot access;
- unauthorized project or allocation existence;
- candidate roots before entitlement and path validation;
- group membership details;
- numeric UID/GID diagnostics;
- ACL entries;
- endpoint credentials or tokens;
- internal service URLs not intended as public endpoint metadata.

Detailed allocation IDs, paths, and identity diagnostics belong in access-controlled
server logs, not client errors.

## Cross-repository changes

### `iri-facility-api-python`

Completed for the initial static scope because this repository owns the IRI contract
and S3DF adapter:

- add `app/s3df/storage_adapter.py`;
- export `S3DFStorageAdapter`;
- wire the adapter in S3DF runtime configuration;
- add focused adapter tests.

Deferred with dynamic discovery:

- add typed storage policy/config parsing;
- harden Coact storage queries so failures remain failures;
- extend the fs-facade client for effective-access checks;
- add route, client, configuration, and failure tests for dynamic resources.

### `fs-facade-service`

Required only for effective access. The API cannot safely compute POSIX/ACL access
from Coact records, UID/GID headers, or `stat` mode bits.

- add a read-only effective-access command;
- run it after the existing UID/GID privilege drop;
- return typed existence/read/write/execute results;
- preserve canonical-root validation;
- test owner, group, ACL where available, denied traversal, missing path, malformed
  identity, and root escape.

No other storage-discovery policy should move into fs-facade.

### `iri-deploy`

No identity-header or fs-facade change is required by static `sdfhome` discovery.
Deployment must expose and test the configured v2 storage route. The following work
is required only before dynamic discovery is enabled:

- reconcile ForwardAuth response headers with API/fs-facade consumers;
- document the UID/GID producer and encoding;
- enable strict identity requirements in fs-facade;
- provide storage policy and endpoint configuration;
- ensure the fs-facade pod has only the Linux privileges needed for the existing
  UID/GID drop design.

### `coact/coact-api`

No server change is assumed initially. Existing storage allocations already expose
the fields needed for candidate-path discovery.

A Coact change is justified only if validation shows that the existing API cannot
provide one of:

- user-scoped, non-enumerating repo/allocation access;
- stable public repo/allocation identifiers;
- authoritative `rootfolder`;
- unambiguous `storagename`/`purpose` mapping;
- authoritative scratch allocation roots;
- explicit allocation lifecycle state.

The facility-wide `reportFacilityStorage` query is not an authorization source and
must not be used by `/storage`.

## Testing strategy

### API unit tests

- accept only `sdfhome`, `sdfdata`, and `sdfscratch`;
- reject compute resources and `sdfk8s`;
- resolve a valid home path;
- reject invalid username/path-policy inputs;
- map Coact allocation roots without concatenating filters;
- validate project and allocation filter exclusivity;
- preserve Coact errors;
- enforce canonical roots and symlink policy;
- map fs-facade access results to `StorageInstance.access`;
- test every intent;
- deduplicate and deterministically order results;
- enforce result limits;
- return an empty endpoint registry;
- filter configured endpoints by resource, protocol, and endpoint ID;
- reject invalid endpoint configuration.

### fs-facade tests

- effective owner access;
- effective supplementary-group access;
- read-only and write-denied paths;
- parent traversal denial;
- missing path;
- symlink escape;
- malformed/missing UID/GIDs in strict mode;
- privilege-drop failure;
- typed response and error mapping.

Extended ACL behavior should be covered in an environment where ACLs are supported;
otherwise it remains a deployment-level test.

### Route and contract tests

- `/storage` is absent when the adapter env var is unset;
- routes appear when the S3DF adapter is configured;
- unknown resources retain the router-level `404`;
- explicit missing logical name retains the router-level `404`;
- adapter errors map to documented HTTP statuses;
- OpenAPI responses contain no credentials or undeclared fields.

### Dev service-level tests

- discover the caller's own home;
- discover an entitled project root;
- discover an entitled scratch root if Coact provides one;
- do not enumerate another user's project;
- distinguish read and write intent;
- fail closed for missing/malformed identity;
- reject `sdfk8s` and compute resources;
- surface Coact and fs-facade outages;
- return `[]` for access endpoints.

## Concurrency and performance

- Coact repo/allocation requests and fs-facade access checks are network calls and
  must use bounded timeouts.
- Do not issue unbounded concurrent fs-facade checks. Use a small configured
  concurrency limit or bounded gather operation.
- Cache only static validated policy and endpoint configuration.
- Do not cache user authorization or effective filesystem access in the initial
  release; both can change independently.
- Deduplicate candidate paths before fs-facade calls.
- Enforce a result/candidate limit before fan-out.
- Reuse the existing async HTTP client patterns and close clients during application
  shutdown where supported.

## Observability

Record:

- resource ID and filter presence;
- candidate, returned, inaccessible, and invalid counts;
- Coact and fs-facade latency;
- upstream error class and status;
- endpoint registry size at startup;
- policy/configuration validation failures.

Do not log bearer tokens, passwords, endpoint credentials, full UID/GID lists, or
unauthorized paths. Path logging should be minimized and access-controlled.

## Rollout

For the initial static scope:

1. Run focused adapter and route tests in a dependency-complete environment.
2. Deploy the configured adapter to dev and probe `/api/v2/storage` with `sdfhome`,
  unsupported resources, filters, and invalid identities.
3. Confirm that no CoAct or fs-facade requests occur.
4. Promote the same configuration shape to production.

Before dynamic discovery is added, review and approve the open identity, CoAct, path,
scratch, health, ACL, and endpoint decisions; implement the required effective-access
service; and run cross-repository security and failure tests.

Rollback removes `IRI_API_ADAPTER_storage`, which hides `/storage` without
affecting account, compute, filesystem, status, or task routes.

## Release gates

The following gates apply to dynamic discovery beyond the implemented static
`sdfhome` scope:

- the numeric identity-header contract is authoritative and tested;
- home path normalization and symlink policy are approved;
- Coact `rootfolder`, identifier, and storage-tier semantics are approved;
- scratch has an authoritative root mapping or is removed from the initial release;
- fs-facade evaluates effective access under the intended identity;
- canonical-root escape tests pass;
- unauthorized filters do not enumerate projects or allocations;
- Coact/fs-facade failures are not returned as empty success;
- endpoint configuration is empty or contains only reviewed infrastructure;
- `/filesystem` continues to enforce every operation independently;
- `/storage` remains hidden when its adapter is not configured.

## Open decisions

| Decision | Recommended default | Owner |
| --- | --- | --- |
| Authoritative UID/GID header names and encoding | One numeric UID header plus comma-separated GIDs; fail closed | Auth/deploy owners |
| Primary GID representation | Include it explicitly and document whether it is duplicated in GIDs | Auth/fs owners |
| Spoofed-header removal | Gateway removes client values before ForwardAuth injection | Auth/deploy owners |
| Username-to-UID binding | Guaranteed by the identity service | Auth owners |
| Home username normalization and sharding | Match the actual S3DF account service | Storage/account owners |
| Home/allocation symlink policy | Allow only when canonical target remains in the approved resource root | Storage owners |
| Public `project` identifier | Coact repo ID | Coact/IRI owners |
| Public `allocation` identifier | Coact storage allocation ID | Coact/IRI owners |
| Coact `rootfolder` authority | Treat as authoritative only after owner confirmation | Coact/storage owners |
| `storagename` and `purpose` mapping | Explicit configured mapping, never string guessing | Coact/storage owners |
| Unauthorized explicit filter response | Non-enumerating `404` | Security/API owners |
| Missing but entitled directory | Omit/`404`; provisioning is out of scope | Storage owners |
| Coact entitlement but filesystem denial | Filesystem result wins; omit/`404` | Security/storage owners |
| Scratch retention | Leave `purge_policy_days` unset until enforcement is confirmed | Storage owners |
| Resource-down discovery | Continue discovery; health stays in `/status` | API/operations owners |
| Remote access protocols | Empty registry until infrastructure inventory is complete | Storage/network owners |

## Future work

- Add approved access endpoints after cross-functional infrastructure inventory.
- Model archive/HPSS only after its discovery and authorization workflow is
  defined.
- Add `shared` or `campaign` logical tiers when Coact `purpose` semantics are
  authoritative.
- Revisit capacity/usage only through an intentional generic IRI model change.
- Consider short-lived authorization caching only after correctness and revocation
  requirements are understood.
