FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi ./pi

EXPOSE 8050
CMD ["python", "-m", "uvicorn", "pi.api:app", "--host", "0.0.0.0", "--port", "8050"]
