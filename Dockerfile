FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY simple_bot/ ./simple_bot/
COPY database/ ./database/
COPY models/ ./models/
COPY backtesting/ ./backtesting/
# NOTE: .env NOT copied - provided via docker-compose env_file

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command - run the bot
CMD ["python", "-m", "simple_bot.main"]
