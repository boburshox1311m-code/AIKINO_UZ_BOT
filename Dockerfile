FROM aiogram/telegram-bot-api:latest

USER root
RUN apk add --no-cache \
    python3 \
    py3-pip \
    python3-dev \
    gcc \
    musl-dev \
    postgresql-dev \
    netcat-openbsd \
    tzdata

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir -r /app/requirements.txt

COPY . /app
COPY docker-start.sh /usr/local/bin/aikinouz-start
RUN chmod +x /usr/local/bin/aikinouz-start

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/local/bin/aikinouz-start"]
