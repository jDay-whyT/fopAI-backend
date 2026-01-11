FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY services/processor ./services/processor

CMD ["uvicorn", "services.processor.main:app", "--host", "0.0.0.0", "--port", "8080"]
