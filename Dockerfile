# Dockerfile for Render / generic Docker
FROM python:3.11-slim

WORKDIR /app

# System deps (ffmpeg optional)
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TZ="Asia/Kolkata"

EXPOSE 10000

CMD ["python", "main.py"]
