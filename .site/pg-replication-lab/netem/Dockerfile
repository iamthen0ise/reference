FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent.py partition.py ./

# root осознанно: tc и iptables нужен CAP_NET_ADMIN. В compose у контейнера сняты все capabilities, кроме NET_ADMIN.
EXPOSE 9000
CMD ["python", "agent.py"]
