FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 10001 --create-home app
WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY --chown=app:app pyproject.toml ./
COPY --chown=app:app lab ./lab
COPY --chown=app:app tests ./tests
USER app

# Раннер запускают командой docker compose exec/run runner python -m lab ...; сам контейнер только ждёт.
CMD ["sleep", "infinity"]
