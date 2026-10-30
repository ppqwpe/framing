FROM python:3.14-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/web
COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home framing && mkdir /data && chown framing:framing /data
# 빌드 기준 폴더는 app/ (compose.yaml) — 웹 서버와 공용 엔진(framing 패키지)을 함께 넣는다
COPY --chown=framing:framing web/ /app/web/
COPY --chown=framing:framing framing/ /app/framing/
USER framing
ENV FRAMING_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8000","--workers","1","--proxy-headers","--forwarded-allow-ips","*"]
