FROM python:3.12-slim

# Cross-platform: the same image runs identically on a Pi, a NAS, or a VPS.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY mcpsgradewatch ./mcpsgradewatch
RUN pip install --no-cache-dir .[service]

# Runtime state (db + snapshots) lives on a mounted volume, not in the image.
VOLUME ["/data"]
ENV MCPSGRADEWATCH_DB_PATH=/data/mcpsgradewatch.db \
    MCPSGRADEWATCH_SNAPSHOT_DIR=/data/snapshots \
    MCPSGRADEWATCH_SECRET_BACKEND=env

# In Docker the password comes from a secret store into MCPSGRADEWATCH_PASSWORD;
# never bake credentials into the image or compose file.
ENTRYPOINT ["mcpsgradewatch"]
CMD ["preflight"]
