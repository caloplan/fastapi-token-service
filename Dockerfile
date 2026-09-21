FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.ustc.edu.cn/pypi/simple/ --trusted-host mirrors.ustc.edu.cn

COPY . .

RUN mkdir -p /app/logs

EXPOSE 9096

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9096"]
