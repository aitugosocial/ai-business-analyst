# Dockerfile for Lavoo Intelligence Cron Job
# Generates a unified feed of insights + opportunities in one run.

FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the Lavoo Intelligence cron job
CMD ["python", "cron/intelligence/intelligence.py"]
