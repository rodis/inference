FROM python:3.13-slim

WORKDIR /app

# Install the inference package and its dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Copy the worker entrypoint under its directory so Path(__file__).parent.name
# resolves to the worker name (car_door_opened), matching the repo layout.
COPY workers/car_door_opened/main.py ./workers/car_door_opened/main.py

RUN useradd -u 1000 -m worker
USER 1000

CMD ["python", "workers/car_door_opened/main.py"]
