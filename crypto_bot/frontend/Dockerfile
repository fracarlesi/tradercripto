FROM python:3.11-slim

WORKDIR /app

COPY crypto_bot/frontend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy frontend package only — accessor reads JSONL from a mounted volume.
COPY crypto_bot/frontend /app/crypto_bot/frontend
COPY crypto_bot/__init__.py /app/crypto_bot/__init__.py

ENV PYTHONPATH=/app
ENV HLQUANTBOT_DATA_DIR=/data

EXPOSE 5611

CMD ["python", "-m", "crypto_bot.frontend.main"]
