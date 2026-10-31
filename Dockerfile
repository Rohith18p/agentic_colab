FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/workspace.db

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md ./
COPY agentic_colab ./agentic_colab
COPY runner ./runner
RUN pip install --no-cache-dir --no-deps .

VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "agentic_colab", "--host", "0.0.0.0", "--port", "8000"]
