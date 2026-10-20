FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOTA_ANALYTICS_DB_PATH=/data/history.sqlite3

WORKDIR /srv/app

COPY requirements-web.txt ./
RUN python -m pip install --no-cache-dir -r requirements-web.txt

COPY app/ ./app/

RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
