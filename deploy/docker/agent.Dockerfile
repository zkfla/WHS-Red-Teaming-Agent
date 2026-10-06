FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[llm]"

RUN useradd --create-home --uid 10001 redteam \
    && mkdir -p /app/.redteaming /app/reports \
    && chown -R redteam:redteam /app
USER redteam

ENTRYPOINT ["python", "-m", "redteaming.cli"]
