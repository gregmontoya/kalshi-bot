FROM python:3.12-slim

WORKDIR /app

# Security: Run as non-root quant user in isolated container
RUN useradd -m -u 1000 quantbot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pyyaml

COPY . .
RUN chown -R quantbot:quantbot /app

USER quantbot

EXPOSE 8000

# Starts both the Dashboard API (/tracker and /crypto) and the 24/7 Crypto Quant Roller
CMD ["sh", "-c", "uvicorn src.api.server:app --host 0.0.0.0 --port ${PORT:-8000} & python scripts/crypto_24_7_roller.py"]
