FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY crypto_bot/ ./crypto_bot/
COPY models/ ./models/
COPY backtesting/ ./backtesting/
# NOTE: .env NOT copied - provided via docker-compose env_file

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command - run the bot
CMD ["python", "-m", "crypto_bot.main"]
