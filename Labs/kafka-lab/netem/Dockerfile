FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent.py cuts.py ./

# root осознанно: iptables нужен CAP_NET_ADMIN. В compose у контейнера сняты все capabilities, кроме NET_ADMIN.
EXPOSE 9000
CMD ["python", "agent.py"]
