FROM python:3.11-slim

# System dependencies required by WeasyPrint (PDF generation) and PyMuPDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 \
    libffi-dev shared-mime-info fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user. MongoDB stores durable data; /app/data only
# contains regenerable cache/temp files and must remain writable.
RUN mkdir -p /app/data /app/data/cache /app/data/tmp \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin quizbot \
    && chown -R quizbot:quizbot /app
USER quizbot

EXPOSE 8080
CMD ["python", "run.py"]
