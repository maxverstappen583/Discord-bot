# Use official Python image
FROM python:3.11

# Set working directory
WORKDIR /app

# Copy dependency list
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY . .

# Set environment variable for Python to not buffer logs
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "main.py"]
