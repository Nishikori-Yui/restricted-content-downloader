# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Build a wheel in a disposable stage. The final image never receives the
# build toolchain or the repository checkout.
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 10001 tgdownloader \
    && adduser --system --uid 10001 --gid 10001 --home /app tgdownloader \
    && mkdir -p /app/logs \
    && chown -R tgdownloader:tgdownloader /app

COPY --from=builder /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-compile /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

USER tgdownloader

CMD ["python", "-m", "tg_downloader"]
