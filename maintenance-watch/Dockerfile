FROM python:3.12-slim
WORKDIR /app/maintenance-watch
COPY upload-requirements.txt /app/upload-requirements.txt
RUN pip install --no-cache-dir "psycopg[binary]==3.2.9" -r /app/upload-requirements.txt \
    && useradd --create-home --uid 10001 monitor
COPY telemetry_files.py /app/telemetry_files.py
COPY maintenance-watch/monitor.py maintenance-watch/scoring.py maintenance-watch/storage.py maintenance-watch/storage_schema.sql ./
COPY maintenance-watch/postgres/powerbi.sql ./postgres/powerbi.sql
COPY maintenance-watch/artifacts/ ./artifacts/
COPY maintenance-watch/dashboard/ ./dashboard/
COPY maintenance-watch/data/replay.csv ./data/replay.csv
USER monitor
EXPOSE 8000
ENTRYPOINT ["python", "-u", "monitor.py", "--bind", "0.0.0.0"]
CMD ["--demo"]
