"""
S3DF Configuration

Environment-driven settings for connecting to S3DF services.
"""

import os
from dataclasses import dataclass


@dataclass
class S3DFSettings:
    """Configuration for S3DF coact-api integration."""
    
    def __init__(self):
        self.coact_api_url = os.getenv("COACT_API_URL", "https://coact-dev.slac.stanford.edu/graphql-service-dev")
        self.coact_service_user = os.getenv("COACT_SERVICE_USER")
        self.coact_service_password = os.getenv("COACT_SERVICE_PASSWORD")
        self.coact_use_basic_auth = True

        # Facility identification
        self.facility_name = os.getenv("S3DF_FACILITY_NAME", "s3df")


        # HTTP header coact-api uses to identify the caller.
        # 'coactimp' is the impersonation header read by the coact Strawberry context;
        # set to a username to query as that user, or leave empty to use the server default.
        self.coact_auth_header = os.getenv("COACT_AUTH_HEADER", "coactimp")

        # Dex IdP — JWKS-based JWT verification for incoming Bearer tokens.
        self.dex_jwks_url = os.getenv("DEX_JWKS_URL")
        self.dex_issuer = os.getenv("DEX_ISSUER")
        # Comma-separated list of accepted audiences (e.g. "aud1,aud2").
        _raw_audience = os.getenv("DEX_AUDIENCE", "")
        self.dex_audience: list[str] = [
            a.strip() for a in _raw_audience.split(",") if a.strip()
        ]
        self.dex_username_claim = os.getenv("DEX_USERNAME_CLAIM", "name")

        # user-lookup service (direct LDAP/POSIX identity queries)
        self.user_lookup_url = os.getenv("USER_LOOKUP_URL", "")

        # fs-facade-service (filesystem operations microservice)
        self.fs_facade_url = os.getenv("FS_FACADE_URL", "http://fs-facade-service:8100")
        self.fs_facade_poll_interval = float(os.getenv("FS_FACADE_POLL_INTERVAL", "0.25"))
        self.fs_facade_timeout = float(os.getenv("FS_FACADE_TIMEOUT", "60"))

        # s3df-status-api (status microservice)
        self.s3df_status_api_url = os.getenv("S3DF_STATUS_API_URL", "http://s3df-status-api:8080")
        self.s3df_status_api_timeout = float(os.getenv("S3DF_STATUS_API_TIMEOUT", "10"))


settings = S3DFSettings()
