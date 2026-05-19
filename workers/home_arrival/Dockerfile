FROM python:3.13-slim

WORKDIR /app

# Install the inference package and its dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Copy the worker entrypoint under its directory so Path(__file__).parent.name
# resolves to the worker name (home_arrival), matching the repo layout.
COPY workers/home_arrival/main.py ./workers/home_arrival/main.py

RUN useradd -u 1000 -m worker
USER 1000

CMD ["python", "workers/home_arrival/main.py"]
