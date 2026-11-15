# Container for the frozen-forecast web service (Phase 7).
# Build it from the small staging folder described in service/README.md, not from the repo root:
# the repo's data/ folder is several GB and must not be sent to Docker.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forecast_api.py sample_history.csv.gz ./
COPY src/demand_features.py src/demand_features.py
COPY model/frozen_model.joblib model/frozen_choice.json model/

ENV FORECAST_MODEL_PATH=/app/model/frozen_model.joblib \
    FORECAST_CHOICE_PATH=/app/model/frozen_choice.json \
    FORECAST_HISTORY_PATH=/app/sample_history.csv.gz \
    PYTHONUNBUFFERED=1

RUN useradd --create-home service_user
USER service_user

EXPOSE 8000
CMD ["uvicorn", "--factory", "forecast_api:create_app_from_environment", "--host", "0.0.0.0", "--port", "8000"]
