FROM python:3.12-slim

# Cross-platform: the same image runs identically on a Pi, a NAS, or a VPS.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY gradewatch ./gradewatch
RUN pip install --no-cache-dir .[service]

# Runtime state (db + snapshots) lives on a mounted volume, not in the image.
VOLUME ["/data"]
ENV GRADEWATCH_DB_PATH=/data/gradewatch.db \
    GRADEWATCH_SNAPSHOT_DIR=/data/snapshots \
    GRADEWATCH_SECRET_BACKEND=env

# In Docker the password comes from a secret store into GRADEWATCH_PASSWORD;
# never bake credentials into the image or compose file.
ENTRYPOINT ["gradewatch"]
CMD ["preflight"]
