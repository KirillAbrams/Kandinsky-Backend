# Собирает общий образ для основного сервера, службы связи и обработчика заданий.
# Библиотеки берутся из закреплённого списка, а служба запускается под обычным пользователем.

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 studio
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip==26.3 && pip install --no-cache-dir -r requirements.lock

COPY app/ ./app/
COPY bootstrap/ ./bootstrap/
COPY config/ ./config/
COPY routes/ ./routes/
COPY database/ ./database/
COPY alembic.ini ./
RUN mkdir -p /media /state && chown studio:studio /media /state

USER studio
EXPOSE 8098
CMD ["uvicorn", "bootstrap.studio:app", "--host", "0.0.0.0", "--port", "8098", "--no-access-log"]
