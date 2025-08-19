# Dockerfile for running on Render
FROM python:3.11-slim

WORKDIR /app

# Install system deps (for some Python packages)
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential curl git \
  && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Expose port for Flask keepalive
EXPOSE 8080

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
