import argparse
import asyncio
import json
import logging
import os

from app.s3df.clients.user_lookup import UserLookupClient

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)


def normalize_base_url(url: str) -> str:
    base_url = url.rstrip("/")
    if base_url.endswith("/graphql"):
        base_url = base_url[: -len("/graphql")]
    return base_url


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test the user-lookup client.")
    parser.add_argument("username", help="Username to query")
    parser.add_argument(
        "--url",
        default=os.getenv("USER_LOOKUP_URL"),
        help="Base user-lookup URL. You can also set USER_LOOKUP_URL.",
    )
    args = parser.parse_args()

    if not args.url:
        LOG.error("Set USER_LOOKUP_URL or pass --url")
        return 2

    client = UserLookupClient(url=normalize_base_url(args.url))

    try:
        result = await client.get_user(args.username)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:
        LOG.exception("user-lookup request failed: %s", exc)
        return 3

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))