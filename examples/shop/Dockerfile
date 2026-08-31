FROM python:3.12-slim AS base

WORKDIR /app

RUN groupadd --system shop && useradd --system --gid shop --no-create-home shop

COPY pyproject.toml ./
COPY README.md ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY shop ./shop

RUN pip install --no-cache-dir .

USER shop

EXPOSE 8000

CMD ["uvicorn", "shop.main:app", "--host", "0.0.0.0", "--port", "8000"]
