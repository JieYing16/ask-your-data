FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install the DuckDB sqlite extension at build time so the container
# doesn't need outbound network access to load it at runtime.
RUN python -c "import duckdb; con = duckdb.connect(); con.execute('INSTALL sqlite')"

COPY app ./app
COPY static ./static
COPY data ./data

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
