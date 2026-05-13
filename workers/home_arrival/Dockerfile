FROM python:3.13-slim

WORKDIR /app

# Install the inference package and its dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Copy the worker entrypoint
COPY workers/home_arrival/main.py ./main.py

CMD ["python", "main.py"]
