# Use official Python runtime as a base image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install system dependencies (for pytz and Discord voice if you add later)
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt first (better caching for Docker)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code
COPY . .

# Environment variables (set defaults, can be overridden in Render)
ENV TZ="Asia/Kolkata"

# Run the bot
CMD ["python", "main.py"]
