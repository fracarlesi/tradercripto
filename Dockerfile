FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (libgomp for LightGBM)
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU-only (saves ~1.5GB vs full CUDA version)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY crypto_bot/ ./crypto_bot/
COPY models/ ./models/
COPY backtesting/ ./backtesting/
# NOTE: .env NOT copied - provided via docker-compose env_file

# Create directories for FLAG-Trader data and models
RUN mkdir -p data/candles models/flag_trader

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command - run the bot
CMD ["python", "-m", "crypto_bot.main"]
