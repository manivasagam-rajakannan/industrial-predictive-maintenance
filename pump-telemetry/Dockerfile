FROM python:3.12-slim AS base
WORKDIR /app/pump-telemetry
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS backend
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY pump-telemetry/backend/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY pump-telemetry/backend ./backend
COPY pump-telemetry/rag ./rag
COPY pump-telemetry/model/__init__.py pump-telemetry/model/pump_pipeline.py pump-telemetry/model/live_views.sql ./model/
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS frontend
COPY upload-requirements.txt /app/upload-requirements.txt
COPY pump-telemetry/frontend/requirements.txt ./frontend/requirements.txt
RUN python -m pip install --no-cache-dir -r frontend/requirements.txt
COPY telemetry_files.py /app/telemetry_files.py
COPY pump-telemetry/frontend ./frontend
COPY pump-telemetry/.streamlit ./.streamlit
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
