# Dockerfile for Render
FROM python:3.11-slim

WORKDIR /app

# system deps for ffmpeg or audioops if needed (voice features)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
