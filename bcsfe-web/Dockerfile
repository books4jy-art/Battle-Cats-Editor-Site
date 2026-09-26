FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py worker.py extras.py gate.py passwords.py ./
COPY static ./static
ENV BCSFE_DATA_DIR=/tmp/bcsfe-data PORT=8000 TRUST_PROXY=1
EXPOSE 8000
CMD ["python", "app.py"]
