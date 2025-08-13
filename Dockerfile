# Base image: official Python slim
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy all files
COPY . /app

# Upgrade pip and install dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Set environment variables for Render
ENV PYTHONUNBUFFERED=1
ENV DISCORD_BOT_TOKEN=your_token_here

# Expose Flask port
EXPOSE 8080

# Healthcheck (optional for Render)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -f http://localhost:8080/ || exit 1

# Run the bot
CMD ["python", "main.py"]
