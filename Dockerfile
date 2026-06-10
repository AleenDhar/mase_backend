# syntax=docker/dockerfile:1
#
# MASE service container image (DeepAgent FastAPI server).
# Multi-stage: deps are compiled/installed in a builder venv, then copied into a
# slim runtime so the final image stays small and free of build tools.
#
# NOTE on the corporate proxy: this machine sits behind a Zscaler TLS-inspecting
# proxy. deploy.ps1 drops the corporate root CA(s) into ./build-certs/ before the
# build so pip can fetch packages over the intercepted TLS. The CA is used ONLY in
# the builder stage and is NOT carried into the runtime image. On a network without
# interception, build-certs/ just contains .gitkeep and this is a no-op.

############################ builder ############################
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        pkg-config \
        ca-certificates \
        curl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv we can copy wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

############################ runtime ############################
FROM python:3.11-slim AS runtime

# Minimal runtime libraries (wheels already bundle most native code), plus
# Node.js 20 — several MCP integration servers are launched via `npx`
# (apollo, zerobounce, smartlead).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        libffi8 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Bring in the prebuilt dependency venv.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=5000

WORKDIR /app
COPY . .

# Run as a non-root user.
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Container-level healthcheck mirrors the ALB target-group check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5000/api/health || exit 1

# Default command runs the API/web server. The SQS worker service (added in the
# refactor phase) will override this with its own command in its task definition.
CMD ["python", "server.py"]
