# Use Python 3.11 base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first (better caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

# Expose Flask port (Render expects 10000 by default)
EXPOSE 10000

# Start both Flask and the bot
CMD ["python", "main.py"]
