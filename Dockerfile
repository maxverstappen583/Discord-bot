# Use an official Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt and install dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot’s code
COPY . .

# Expose Flask port (Render expects 10000 by default for free web services)
EXPOSE 10000

# Run both Flask keepalive and your bot
CMD ["python", "main.py"]
