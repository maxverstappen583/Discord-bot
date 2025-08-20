# =========================
# Dockerfile
# =========================
FROM python:3.11-slim

WORKDIR /app

# Prevents Python from buffering stdout/stderr (useful for logs)
ENV PYTHONUNBUFFERED=1

# Install system deps (optional: for psutil wheels are fine; keep slim)
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# Render will set $PORT; Flask uses it; Discord bot runs in same process.
# Expose for health checks (optional locally)
EXPOSE 8080

CMD ["python", "main.py"]
