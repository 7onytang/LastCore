# Official Python OCI linux/amd64 manifest resolved 2026-10-13; immutable digest.
FROM python:3.12-slim@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
COPY ops/staging/requirements-linux-lock.txt requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt \
    && groupadd --gid 10001 controller \
    && useradd --uid 10001 --gid controller --no-create-home controller \
    && mkdir /data && chown controller:controller /data
COPY controller-server/app ./app
COPY LICENSE /app/LICENSE
USER controller
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000", "--workers", "1"]
