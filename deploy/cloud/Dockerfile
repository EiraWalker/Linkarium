# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ARG LINKARIUM_VERSION=local
ARG LINKARIUM_REVISION=unknown
LABEL org.opencontainers.image.title="Linkarium" \
      org.opencontainers.image.source="https://github.com/EiraWalker/Linkarium" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version=$LINKARIUM_VERSION \
      org.opencontainers.image.revision=$LINKARIUM_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOOKMARK_DATA_ROOT=/data
WORKDIR /app
COPY deploy/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 bookmark \
    && useradd --uid 10001 --gid bookmark --no-create-home --shell /usr/sbin/nologin bookmark
COPY backend/ ./backend/
COPY frontend/dist/ ./frontend/dist/
COPY LICENSE ./LICENSE
USER 10001:10001
EXPOSE 8000
CMD ["python", "backend/supervisor.py", "--host", "0.0.0.0", "--port", "8000"]
