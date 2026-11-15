FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10002 paymentdemo \
    && useradd --uid 10002 --gid paymentdemo --home-dir /app --no-create-home --shell /usr/sbin/nologin paymentdemo
COPY app.py .
USER 10002:10002
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
