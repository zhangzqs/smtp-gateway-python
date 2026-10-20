FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.11-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src/ ./src/
RUN uv export --locked --no-dev --no-emit-project --no-hashes \
        --output-file /tmp/requirements.txt \
    && uv build --wheel --out-dir /dist

FROM python:3.11-slim AS runtime
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=builder /tmp/requirements.txt /tmp/requirements.txt
COPY --from=builder /dist/ /tmp/wheels/
RUN uv pip install --system --no-cache --no-deps \
        -r /tmp/requirements.txt /tmp/wheels/*.whl \
    && mkdir /data \
    && chown 10001:10001 /data
USER 10001:10001
WORKDIR /data
EXPOSE 8080 1025
ENTRYPOINT ["smtp-gateway"]
