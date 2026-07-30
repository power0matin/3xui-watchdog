# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --break-system-packages "."

FROM python:3.12-slim

RUN useradd --system --no-create-home --shell /usr/sbin/nologin watchdog

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/xui-watchdog /usr/local/bin/xui-watchdog

WORKDIR /app
USER watchdog

ENTRYPOINT ["xui-watchdog"]
CMD ["--config", "/app/config.yaml"]
