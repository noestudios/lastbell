FROM python:3.12-slim

# Cross-platform: the same image runs identically on a Pi, a NAS, or a VPS.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY lastbell ./lastbell
RUN pip install --no-cache-dir .[service]

# Runtime state (db + snapshots) lives on a mounted volume, not in the image.
VOLUME ["/data"]
ENV LASTBELL_DB_PATH=/data/lastbell.db \
    LASTBELL_SNAPSHOT_DIR=/data/snapshots \
    LASTBELL_SECRET_BACKEND=env

# In Docker the password comes from a secret store into LASTBELL_PASSWORD;
# never bake credentials into the image or compose file.
ENTRYPOINT ["lastbell"]
CMD ["preflight"]
