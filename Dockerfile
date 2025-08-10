# Use official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot's code
COPY . .

# Expose port 8080 for Render health checks (if you have a web server)
EXPOSE 8080

# Run your bot
CMD ["python", "main.py"]
