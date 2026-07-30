FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
COPY pyproject.toml README.md .env.example .env.production.example ./
RUN pip install --no-cache-dir -e ".[dev]"

COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY alembic.ini ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
