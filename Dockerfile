# Use official Python image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Copy current directory contents
COPY . /app

# Install system dependencies for building some Python packages
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip

# Install Python dependencies
RUN pip install -r requirements.txt

# Expose Flask port
EXPOSE 8080

# Start bot
CMD ["python", "main.py"]