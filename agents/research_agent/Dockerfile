# 1. Base Image: Lightweight Python (No Browser needed)
FROM python:3.11-slim

WORKDIR /app

# 2. Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. App Code
COPY . .

# 4. Run
EXPOSE 8001
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]
