# PolicyPulse checker (FastAPI /check) and the Streamlit app share this image.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY data/sample ./data/sample
COPY cache ./cache
COPY .streamlit ./.streamlit
# Build the KB index from the bundled sample PDFs (mount your own library and set KB_PATH to use it).
RUN python ingest.py
EXPOSE 8000 8501
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
