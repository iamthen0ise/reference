import unittest

from docker_control import DockerControl, DockerError

CONTAINERS = [
    {"Id": "abc123", "State": "running", "Status": "Up 5 minutes",
     "Labels": {"com.docker.compose.service": "replica1"}},
    {"Id": "def456", "State": "running", "Status": "Up 5 minutes",
     "Labels": {"com.docker.compose.service": "primary"}},
]


class DockerControlTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.dc = DockerControl("http://proxy:2375", "p", frozenset({"replica1", "loader"}))

        def fake(method, path):
            self.calls.append((method, path))
            if method == "GET":
                return 200, CONTAINERS
            return 204, None

        self.dc._request = fake

    def test_act_rejects_unknown_service(self):
        for svc in ("primary", "dashboard", "docker-proxy", "x"):
            with self.assertRaises(ValueError):
                self.dc.act(svc, "stop")
        self.assertEqual(self.calls, [])

    def test_act_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            self.dc.act("replica1", "remove")
        self.assertEqual(self.calls, [])

    def test_act_kill_uses_sigkill(self):
        self.dc.act("replica1", "kill")
        self.assertEqual(self.calls[-1], ("POST", "/containers/abc123/kill?signal=SIGKILL"))

    def test_act_stop_uses_timeout(self):
        self.dc.act("replica1", "stop")
        self.assertEqual(self.calls[-1], ("POST", "/containers/abc123/stop?t=10"))

    def test_act_restart_uses_timeout(self):
        self.dc.act("replica1", "restart")
        self.assertEqual(self.calls[-1], ("POST", "/containers/abc123/restart?t=10"))

    def test_states_malformed_element(self):
        for bad in ("x", {"State": "running"}, {"Id": 1, "Labels": {}}, {"Id": "a", "Labels": None}):
            self.dc._request = lambda m, p, bad=bad: (200, [bad])
            with self.assertRaises(DockerError):
                self.dc.states()

    def test_act_error_message(self):
        self.dc._request = lambda m, p: (409, {"message": "конфликт"}) if m == "POST" else (200, CONTAINERS)
        with self.assertRaisesRegex(DockerError, "конфликт"):
            self.dc.act("replica1", "start")

    def test_act_not_modified_is_ok(self):
        self.dc._request = lambda m, p: (304, None) if m == "POST" else (200, CONTAINERS)
        self.dc.act("replica1", "start")

    def test_act_missing_container(self):
        with self.assertRaises(DockerError):
            self.dc.act("loader", "start")

    def test_states_maps_by_service_label(self):
        self.assertEqual(self.dc.states()["replica1"],
                         {"state": "running", "status": "Up 5 minutes", "container_id": "abc123"})
        self.assertEqual(self.dc.states()["loader"], {"state": "missing"})
        self.assertNotIn("primary", self.dc.states())

    def test_states_proxy_unreachable(self):
        with self.assertRaises(DockerError):
            DockerControl("http://127.0.0.1:9", "p", frozenset({"replica1"}), timeout=0.5).states()


if __name__ == "__main__":
    unittest.main()
