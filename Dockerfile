FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY attendance_core.py app.py ./
# Cloud Run provides $PORT (defaults to 8080)
ENV PORT=8080
CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true --server.maxUploadSize=400
