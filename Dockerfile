FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM --platform=$BUILDPLATFORM golang:1.26.4-bookworm AS extractor
ARG TARGETOS
ARG TARGETARCH
WORKDIR /build
COPY third_party/xtractr/ ./third_party/xtractr/
COPY extractor/ ./extractor/
WORKDIR /build/extractor
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /build/115work-extract .

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /opt/115work
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
COPY pyproject.toml uv.lock alembic.ini ./
COPY app/ ./app/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir uv==0.13.0 \
    && uv sync --frozen --no-dev --extra p115 --no-editable \
    && mkdir -p /data /opt/115work/frontend/dist \
    && chown app:app /data
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
COPY --from=extractor /build/115work-extract ./bin/115work-extract
COPY THIRD_PARTY_NOTICES.md ./
COPY third_party/licenses/ ./licenses/dependencies/
COPY third_party/UNPACKFLOW_LICENSE ./licenses/UNPACKFLOW_LICENSE
COPY third_party/xtractr/LICENSE ./licenses/XTRACTR_LICENSE
ENV WORK115_DATA_DIR=/data \
    WORK115_ADAPTER=disabled \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENV PATH="/opt/115work/.venv/bin:$PATH"
USER 1000:1000
EXPOSE 5656
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5656/health/ready', timeout=4)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5656", "--workers", "1"]
