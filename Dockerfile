# =============================================
# Dockerfile for WhatsApp Group Cloner Bot
# Fixed for main.py + optimized for Railway / any Docker host
# =============================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    python-telegram-bot==21.6 \
    requests

# Copy the bot script (now using main.py)
COPY main.py /app/main.py

# Optional: Copy admins.json if you want to keep it
# COPY admins.json /app/admins.json

# Create non-root user for security
RUN useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

# The bot reads these environment variables
CMD ["python", "main.py"]
