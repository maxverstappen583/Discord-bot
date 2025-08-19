# Use slim Python 3.11 (has audioop back)
FROM python:3.11-slim

WORKDIR /app

# System deps (optional but useful)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates build-essential curl git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default port for Flask healthcheck (Render will ping it)
ENV PORT=10000

# Start your bot (Flask thread runs inside main.py)
CMD ["python", "main.py"]
