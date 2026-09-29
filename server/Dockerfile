# Tool Server 單機 Compose image；兩個 base image 都使用固定版本與 digest。
FROM docker:29.4.3-cli@sha256:bd856b0b9684b059ef942f0bfe9f3c4246358637a778c29e66e4e74e995ed606 AS docker-cli

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY knowledge/court-zones.json /app/knowledge/court-zones.json

RUN python -m pip install --no-cache-dir . \
    && mkdir -p /var/lib/badminton-ai/runtime \
    && python -m compileall -q /app/src

EXPOSE 8000

CMD ["python", "-m", "badminton_ai.server"]
