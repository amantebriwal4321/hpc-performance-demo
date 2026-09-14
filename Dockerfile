FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY backend/ ./backend/

WORKDIR /app/backend

EXPOSE 8000

# One uvicorn worker: the FastAPI process itself spawns the ProcessPoolExecutor,
# so a single server process must be able to see and use every core. Do NOT add
# --workers here (that would fork the API, not the compute pool).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
