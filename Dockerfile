# noc-agent engine image. Runbooks, config, keys and the audit log are mounted, never baked in:
#   /config/config.yaml        engine config (llm, policy, paths below)
#   /runbooks/runbooks.yaml    the only actions the agent can take
#   /var/lib/noc-agent         audit.jsonl + approvals/ (persist this volume)
#   /root/.ssh                 keys pinned on the gateways (read-only mount)
# openssh-client is the one system package: runbooks reach hosts over forced-command SSH.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY noc_agent ./noc_agent
COPY examples ./examples
RUN pip install --no-cache-dir .

VOLUME ["/var/lib/noc-agent"]
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8088/healthz || exit 1

ENTRYPOINT ["noc-agent", "-c", "/config/config.yaml"]
CMD ["serve"]
