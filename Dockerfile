FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]==3.2.9" \
    && useradd --create-home --uid 10001 monitor
COPY monitor.py scoring.py storage.py storage_schema.sql ./
COPY artifacts/ ./artifacts/
COPY dashboard/ ./dashboard/
COPY data/replay.csv ./data/replay.csv
USER monitor
EXPOSE 8000
ENTRYPOINT ["python", "-u", "monitor.py", "--bind", "0.0.0.0"]
CMD ["--demo"]
