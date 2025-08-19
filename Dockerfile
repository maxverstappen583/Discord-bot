# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (for pip & discord.py)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose Flask port (for keepalive)
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run both Flask keepalive + Discord bot
CMD ["python", "main.py"]
