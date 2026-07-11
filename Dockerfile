FROM python:3.10-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY xchina_downloader.py .

ENV PYTHONUNBUFFERED=1

# Persistent data directory -- mount a Railway volume at this path
CMD ["python", "-u", "xchina_downloader.py"]
