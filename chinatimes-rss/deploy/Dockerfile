# Optional container path; systemd on aiserver is the default deployment.
FROM python:3.12-slim
WORKDIR /opt/ctime-rss
COPY . .
RUN python -m venv .venv && .venv/bin/pip install --no-cache-dir -e .
VOLUME ["/srv/feeds", "/var/lib/ctime-rss", "/var/log/ctime-rss"]
ENTRYPOINT ["/opt/ctime-rss/.venv/bin/ctime-rss"]
CMD ["--config-dir", "/opt/ctime-rss/config", "once"]
