FROM python:3.12-slim
WORKDIR /app/pump-telemetry
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 pump
COPY upload-requirements.txt /app/upload-requirements.txt
COPY pump-telemetry/backend/requirements.txt ./backend/requirements.txt
COPY pump-telemetry/frontend/requirements.txt ./frontend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt -r frontend/requirements.txt
COPY telemetry_files.py /app/telemetry_files.py
COPY pump-telemetry/backend ./backend
COPY pump-telemetry/frontend ./frontend
COPY pump-telemetry/rag ./rag
COPY pump-telemetry/model/__init__.py pump-telemetry/model/pump_pipeline.py pump-telemetry/model/live_views.sql ./model/
COPY pump-telemetry/.streamlit ./.streamlit
COPY pump-telemetry/deploy/start.py ./deploy/start.py
USER pump
EXPOSE 10000
CMD ["python", "deploy/start.py"]
