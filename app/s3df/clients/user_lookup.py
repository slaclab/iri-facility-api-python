"""
user-lookup GraphQL Client

Async client for querying the user-lookup service for POSIX identity data.
No authentication required (user-lookup has open CORS).
"""

import logging
from typing import Optional

from gql import gql, Client
from gql.transport.httpx import HTTPXAsyncTransport

from app.s3df.config import settings

LOG = logging.getLogger(__name__)

_GET_USER_QUERY = gql("""
    query GetUser($filter: UserInput!) {
        users(filter: $filter) {
            username
            uidnumber
            gidNumber
            secondaryGidNumbers
        }
    }
""")


class UserLookupClient:
    """GraphQL client for the user-lookup service."""

    def __init__(self, url: str | None = None):
        self.url = (url or settings.user_lookup_url).rstrip("/") + "/graphql"
        LOG.info(f"Initialized UserLookupClient for endpoint: {self.url}")

    def _get_client(self) -> Client:
        transport = HTTPXAsyncTransport(
            url=self.url,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        return Client(transport=transport, fetch_schema_from_transport=False)

    async def get_user(self, username: str) -> dict:
        """
        Fetch user POSIX identity from user-lookup.

        Returns dict with: username, uidnumber, gidNumber, secondaryGidNumbers.
        Raises ValueError if user not found, or propagates transport errors
        if the service is unreachable.
        """
        client = self._get_client()
        async with client as session:
            result = await session.execute(
                _GET_USER_QUERY,
                variable_values={"filter": {"username": username}},
            )
        users = result.get("users", [])
        if not users:
            raise ValueError(f"User '{username}' not found in user-lookup")
        return users[0]


_default_client: Optional[UserLookupClient] = None


def get_user_lookup_client() -> UserLookupClient:
    """Get or create the singleton UserLookupClient instance."""
    global _default_client
    if _default_client is None:
        _default_client = UserLookupClient()
    return _default_client
