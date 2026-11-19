FROM debian:12-slim

# anchor-fleet is configured via environment variables (see main.go). In the
# MEC Sandbox they come from the scenario "Environment variables" box plus the
# MEEP_* values injected by meep-webhook.

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && apt-get autoremove --purge -y \
    && apt-get autoclean \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY ./bin/anchor-fleet/anchor-fleet /anchor-fleet
COPY entrypoint.sh /
RUN chmod +x /entrypoint.sh /anchor-fleet

# App HTTP server (HTTP_PORT); set the same value as "Port #" in the scenario
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
