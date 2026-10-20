FROM ghcr.io/astral-sh/uv:0.13.3 AS uv

FROM python:3.15.0-slim-trixie AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY packages/ ./packages/
RUN uv export --locked --package smtp-gateway-server --no-dev \
        --no-emit-workspace --no-hashes \
        --output-file /tmp/requirements.txt \
    && uv build --all-packages --wheel --out-dir /dist

FROM python:3.15.0-alpine3.24 AS runtime
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,from=builder,source=/tmp/requirements.txt,target=/tmp/requirements.txt \
    --mount=type=bind,from=builder,source=/dist,target=/tmp/wheels \
    uv pip install --system --no-cache --no-deps --no-binary pyyaml \
        -r /tmp/requirements.txt /tmp/wheels/*.whl \
    && mkdir /data \
    && chown 10001:10001 /data
USER 10001:10001
WORKDIR /data
EXPOSE 8080 1025
ENTRYPOINT ["smtp-gateway"]
