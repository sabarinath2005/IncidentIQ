FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN groupadd --gid 10001 incidentiq \
    && useradd --uid 10001 --gid incidentiq --home-dir /app --no-create-home --shell /usr/sbin/nologin incidentiq
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
