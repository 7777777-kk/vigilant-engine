FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements.lock.txt /app/server/requirements.lock.txt
RUN pip install --no-cache-dir -r server/requirements.lock.txt
COPY server /app/server
RUN useradd --create-home pdf && mkdir -p /app/server/data && chown -R pdf:pdf /app
USER pdf
EXPOSE 8765
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
