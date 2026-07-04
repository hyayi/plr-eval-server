# plr-eval-server — 사내 LAN 전용 (외부 공개 금지: 크롭 = 사적 CCTV 데이터)
#
# 빌드는 lab 저장소 루트에서:  docker build -f server/Dockerfile -t plr-eval-server .
# 채점 코드는 이 이미지에 담긴 lab 소스(evalkit/…)를 쓰며, 업로드된 표면
# 번들의 py는 절대 import/실행하지 않는다 (저장·열람·diff 전용).
FROM python:3.12-slim

WORKDIR /app

# lab 소스 전체 (evalkit·plr_* 채점 표면 포함) — 채점기 버전은 빌드 시점 고정
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt -r server/requirements.txt \
    && git config --global --add safe.directory /app || true

ENV EVAL_SERVER_DATA=/data
VOLUME ["/data"]
EXPOSE 8890

# --workers 1 고정: 쓰기 직렬화가 프로세스 내 asyncio.Lock — 다중 워커 금지.
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8890", "--workers", "1"]
