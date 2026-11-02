FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# запуск не от root
RUN useradd -r -u 10001 app
USER app

EXPOSE 8080
CMD ["python", "-u", "app.py"]
