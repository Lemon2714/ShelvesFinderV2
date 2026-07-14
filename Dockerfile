FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if needed (none strictly required for this project)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend /app/backend

# Copy frontend assets
COPY frontend /app/frontend

# Set working directory to backend for uvicorn
WORKDIR /app/backend

# Expose the app port
EXPOSE 8000

# Set Python path to include backend for imports
ENV PYTHONPATH=/app/backend

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
