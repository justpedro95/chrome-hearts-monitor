FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DB=/data/state.db

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

RUN useradd --create-home --uid 10001 monitor && mkdir -p /data && chown -R monitor /data /app
USER monitor

VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=1m \
  CMD python -c "import os,time,sys; p=os.environ.get('STATE_DB','/data/state.db'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 1800 else 1)"

CMD ["python", "monitor.py"]
