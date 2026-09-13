FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN pip install --no-cache-dir poetry && poetry config virtualenvs.create false && poetry install --only main --no-interaction
COPY jarvis ./jarvis
RUN mkdir -p /data
ENV JARVIS_MEMORY_DB_PATH=/data/jarvis.sqlite3
EXPOSE 8100
CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8100"]
