# Use official Python image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Copy current directory contents into the container
COPY . /app

# Upgrade pip
RUN pip install --upgrade pip

# Install dependencies
RUN pip install -r requirements.txt

# Expose port 8080 for Flask
EXPOSE 8080

# Start the bot
CMD ["python", "main.py"]