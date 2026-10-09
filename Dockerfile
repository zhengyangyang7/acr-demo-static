FROM python:3.12-slim

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY 周度数据更新工具.py official_targets.py 网页服务.py ./
COPY webapp ./webapp

ENV ZHOUDU_BIND=0.0.0.0
ENV ZHOUDU_PORT=8765
EXPOSE 8765

CMD ["python", "网页服务.py"]
