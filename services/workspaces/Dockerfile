FROM python:3.12-slim
WORKDIR /app
ENV PIP_DEFAULT_TIMEOUT=100 PIP_RETRIES=8
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && apt-get update && apt-get install -y --no-install-recommends curl sqlite3 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip for i in 1 2 3 4 5; do pip install -r requirements.txt && break; [ "$i" = 5 ] && exit 1; echo "pip install failed (attempt $i), retrying"; sleep 10; done
COPY . .
CMD ["python","main.py"]
