FROM python:3.13

RUN mkdir /app
COPY . /app
WORKDIR /app

RUN pip install -U pip wheel setuptools uv && \
    uv pip install --system .

ENV IRI_API_ADAPTER_account="app.s3df.account_adapter.S3DFAccountAdapter"
ENV IRI_API_ADAPTER_status="app.s3df.status_adapter.S3DFStatusAdapter"
ENV IRI_API_ADAPTER_compute="app.s3df.compute_adapter.SLACComputeAdapter"
ENV IRI_API_ADAPTER_filesystem="app.s3df.filesystem_adapter.S3DFFilesystemAdapter"
ENV IRI_API_ADAPTER_facility="app.s3df.facility_adapter.S3DFFacilityAdapter"
ENV IRI_API_ADAPTER_task="app.s3df.task_adapter.S3DFTaskAdapter"
ENV IRI_SHOW_MISSING_ROUTES="false"
ENV DEX_JWKS_URL="https://dex.slac.stanford.edu/keys"
ENV DEX_ISSUER="https://dex.slac.stanford.edu"
ENV API_URL_ROOT="https://sdf-iri-dev.slac.stanford.edu"

CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
