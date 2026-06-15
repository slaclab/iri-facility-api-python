# Change Doc: Use coact-api User Impersonation for Authorization

## Problem

`CoactClient.get_user_repos(username)` currently fetches **every** repo from coact-api using the service account and then filters client-side inside iri-api:

```python
# clients/coact.py — current (wrong) approach
async def get_user_repos(self, username: str) -> List[Dict[str, Any]]:
    repos = await self.get_all_repos()           # fetches ALL repos
    user_repos = [repo for repo in repos
                  if username in repo.get("users", [])
                  or username in repo.get("leaders", [])
                  or username == repo.get("principal")]
    return user_repos
```

This means:
- **Authorization is enforced in the wrong layer** — iri-api is doing what coact-api should be responsible for.
- Every call pulls the entire repo collection regardless of how many repos a user actually belongs to.
- The existing user-impersonation feature built into coact-api is unused.

## Root Cause

`CoactClient._get_client()` silently ignores the `username` parameter when `use_basic_auth=True`:

```python
def _get_client(self, username: Optional[str] = None) -> Client:
    if self.use_basic_auth:
        # Only sets Authorization: Basic — username is never used
        headers["Authorization"] = f"Basic {credentials}"
    else:
        # coactimp is only set in the non-basic-auth branch
        headers["coactimp"] = username or "null"
```

The default `CoactClient` (returned by `get_coact_client()`) is created with `use_basic_auth=True`, so the `coactimp` impersonation header is never sent in practice, even though methods like `get_my_repos(username)` pass a username through to `execute_query` → `_get_client`.

## How coact-api Impersonation Works

In `coact-api/main.py` `CustomContext.authn()`:

1. The service account authenticates as a bot/admin user.
2. If the request also includes `coactimp: <username>`, coact-api switches its execution context to that user.
3. `is_impersonating = True` is set, which forces `isadmin = False` on all queries.
4. The `myRepos` query (and the `repos` query) then only return repos where the impersonated user appears in `users`, `leaders`, or `principal` — **server-side filtering**.

The `isImpersonating` field is exposed via `whoami` and `User` GraphQL types confirming the impersonation is active.

## Required Changes

### Change 1 — `app/s3df/clients/coact.py`: `_get_client()`

When `use_basic_auth=True` **and** a `username` is provided, also set the `coactimp` header so coact-api impersonates that user while authenticating the service account via Basic auth.

```python
# Before
if self.use_basic_auth:
    credentials = base64.b64encode(f"{self.service_user}:{self.service_password}".encode()).decode("ascii")
    headers["Authorization"] = f"Basic {credentials}"

# After
if self.use_basic_auth:
    credentials = base64.b64encode(f"{self.service_user}:{self.service_password}".encode()).decode("ascii")
    headers["Authorization"] = f"Basic {credentials}"
    if username:
        headers["coactimp"] = username   # ← impersonate the target user
```

### Change 2 — `app/s3df/clients/coact.py`: `get_user_repos()`

Replace the fetch-all + client-side filter with a call to the already-existing `get_my_repos(username)` method. With Change 1 in place, `get_my_repos` will now correctly set `coactimp` and coact-api will return only the user's repos.

```python
# Before
async def get_user_repos(self, username: str) -> List[Dict[str, Any]]:
    repos = await self.get_all_repos()
    user_repos = [repo for repo in repos
                  if username in repo.get("users", [])
                  or username in repo.get("leaders", [])
                  or username == repo.get("principal")]
    return user_repos

# After
async def get_user_repos(self, username: str) -> List[Dict[str, Any]]:
    return await self.get_my_repos(username)
```

### Change 3 — `app/s3df/account_adapter.py`: `get_projects()`

Remove the stale commented-out membership check that was left from a previous iteration. It is now fully superseded by server-side impersonation. Update the docstring to reflect the new delegation model.

```python
# Before (in get_projects loop)
for repo in repos:
    all_users = set(repo.get("users", []) + repo.get("leaders", []) + [repo.get("principal", "")])
    # if user.id in all_users:   ← remove this dead code

    projects.append(account_models.Project(...))

# After (docstring addition)
"""
coact.Repo → IRI.Project mapping.
coact-api enforces membership via user impersonation (coactimp header);
repos returned are already scoped to the requesting user.
"""
```

## Operational Prerequisite

The service account (`COACT_SERVICE_USER`) must be registered as a **bot user** (`isbot: true`) or **admin** in coact-api's `users` collection for `coactimp` to be honoured. This is an infrastructure/configuration concern, not a code change in iri-api. Without it, the `coactimp` header is rejected and coact-api returns repos for the service account identity instead of the target user.

## Summary of Files to Change

| File | Change |
|------|--------|
| `app/s3df/clients/coact.py` | `_get_client()`: add `coactimp` header in basic-auth branch |
| `app/s3df/clients/coact.py` | `get_user_repos()`: delegate to `get_my_repos(username)` |
| `app/s3df/account_adapter.py` | `get_projects()`: remove dead commented-out membership check; update docstring |
