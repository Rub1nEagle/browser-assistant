#!/usr/bin/env bash
set -e

# supervisord owns the foreground; agent CLI runs separately via
# `docker compose exec agent python -m agent run "..."`.
exec /usr/bin/supervisord -c /opt/docker/supervisord.conf
