FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scenarios ./scenarios

RUN mkdir -p /data

EXPOSE 8000

# Single worker: the write lock / BEGIN IMMEDIATE scheme serialises
# simulations in-process; multiple workers would each be safe too thanks
# to SQLite locks, but caching assumes a single process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
