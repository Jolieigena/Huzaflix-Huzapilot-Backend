FROM python:3.12-slim
WORKDIR /app
ENV PIP_DEFAULT_TIMEOUT=100 PIP_RETRIES=8
RUN groupadd -g 10001 huza && useradd -u 10001 -g huza huza && mkdir /data && chown huza:huza /data
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip for i in 1 2 3 4 5; do pip install -r requirements.txt && break; [ "$i" = 5 ] && exit 1; echo "pip install failed (attempt $i), retrying"; sleep 10; done
COPY . .
USER 10001:10001
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000","--no-access-log"]
