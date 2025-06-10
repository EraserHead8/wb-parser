FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    software-properties-common \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Установка браузера для Playwright
RUN playwright install chromium
RUN playwright install-deps

# Копирование исходного кода
COPY . .

# Создание директории для файлов
RUN mkdir -p parsed_files

# Установка переменных окружения
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Запуск приложения
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 app_simple:app 