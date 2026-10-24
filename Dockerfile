FROM python:3.11-slim

# Install FFmpeg AND Node.js (Required by yt-dlp to decrypt YouTube formats and merge streams)
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -U --pre "yt-dlp[default]"

COPY . .

CMD ["python", "bot.py"]
