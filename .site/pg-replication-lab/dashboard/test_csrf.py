import unittest

from csrf import csrf_error


class CsrfTest(unittest.TestCase):
    def test_rejects_without_header(self):
        self.assertIsNotNone(csrf_error({"Host": "localhost:8088"}))

    def test_rejects_wrong_header_value(self):
        self.assertIsNotNone(csrf_error({"Host": "localhost:8088", "X-Lab-Request": "0"}))

    def test_rejects_foreign_origin(self):
        headers = {"Host": "localhost:8088", "X-Lab-Request": "1", "Origin": "http://evil.example"}
        self.assertIsNotNone(csrf_error(headers))

    def test_accepts_header_without_origin(self):
        self.assertIsNone(csrf_error({"Host": "localhost:8088", "X-Lab-Request": "1"}))

    def test_accepts_same_origin(self):
        headers = {"Host": "localhost:8088", "X-Lab-Request": "1", "Origin": "http://localhost:8088"}
        self.assertIsNone(csrf_error(headers))


if __name__ == "__main__":
    unittest.main()
