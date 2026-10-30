FROM python:3.14-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home shotkit && mkdir /data && chown shotkit:shotkit /data
COPY --chown=shotkit:shotkit . .
USER shotkit
ENV SHOTKIT_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8000","--workers","1","--proxy-headers","--forwarded-allow-ips","*"]
