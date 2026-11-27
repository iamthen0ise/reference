"""Управление контейнерами стенда через docker-socket-proxy (только stdlib).

Белые списки проверяются до любого обращения к Docker: сервис — из `services`, действие — из ALLOWED_ACTIONS.
"""

import http.client
import json
import urllib.parse
from http import HTTPStatus
from typing import Any

ALLOWED_ACTIONS = ("start", "stop", "kill", "restart")
STOP_TIMEOUT_SEC = 10
SERVICE_LABEL = "com.docker.compose.service"
PROJECT_LABEL = "com.docker.compose.project"


class DockerError(Exception):
    pass


class DockerControl:
    def __init__(self, base_url: str, project: str, services: frozenset[str], timeout: float = 3.0) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError(f"ожидается http-адрес прокси Docker: {base_url}")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._project = project
        self._services = services
        self._timeout = timeout
        # stop/restart ждут до STOP_TIMEOUT_SEC завершения процесса — ответ приходит позже обычного таймаута.
        self._post_timeout = timeout + STOP_TIMEOUT_SEC

    def _request(self, method: str, path: str) -> tuple[int, Any]:
        conn = http.client.HTTPConnection(
            self._host, self._port, timeout=self._post_timeout if method == "POST" else self._timeout
        )
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError(f"Docker-прокси недоступен: {exc}") from exc
        finally:
            conn.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = {"message": raw.decode("utf-8", "replace")[:200]}
        return resp.status, data

    def _list(self) -> dict[str, dict]:
        filters = json.dumps({"label": [f"{PROJECT_LABEL}={self._project}"]})
        status, data = self._request("GET", f"/containers/json?all=1&filters={urllib.parse.quote(filters)}")
        if status != HTTPStatus.OK or not isinstance(data, list):
            raise DockerError(_message(data, f"список контейнеров: HTTP {status}"))
        found: dict[str, dict] = {}
        for item in data:
            if not (isinstance(item, dict) and isinstance(item.get("Id"), str) and isinstance(item.get("Labels"), dict)):
                raise DockerError("Docker-прокси вернул контейнер неожиданного вида")
            service = item["Labels"].get(SERVICE_LABEL)
            if service in self._services:
                found[service] = item
        return found

    def states(self) -> dict[str, dict]:
        found = self._list()
        result: dict[str, dict] = {}
        for service in sorted(self._services):
            item = found.get(service)
            if item is None:
                result[service] = {"state": "missing"}
            else:
                result[service] = {
                    "state": item.get("State", ""),
                    "status": item.get("Status", ""),
                    "container_id": item["Id"],
                }
        return result

    def act(self, service: str, action: str) -> None:
        if service not in self._services:
            raise ValueError(f"неизвестный сервис: {service}")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"неизвестное действие: {action}")
        item = self._list().get(service)
        if item is None:
            raise DockerError(f"контейнер сервиса {service} не найден")
        query = {"stop": f"?t={STOP_TIMEOUT_SEC}", "restart": f"?t={STOP_TIMEOUT_SEC}", "kill": "?signal=SIGKILL"}.get(action, "")
        status, data = self._request("POST", f"/containers/{item['Id']}/{action}{query}")
        if not (HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES or status == HTTPStatus.NOT_MODIFIED):
            raise DockerError(_message(data, f"{action} {service}: HTTP {status}"))


def _message(data: Any, default: str) -> str:
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return default
