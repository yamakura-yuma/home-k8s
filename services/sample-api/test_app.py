"""sample-api の経路と OpenAPI の突き合わせ (just ci)。HTTP は立てず route() を直に呼ぶ。"""

import json
import re
import unittest

from app import OPENAPI, route


class RouteTest(unittest.TestCase):
    def test_info_carries_environment(self):
        status, ctype, body = route("/info", "", "dev")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body)["environment"], "dev")

    def test_hello_reads_name(self):
        _, _, body = route("/hello", "name=prod-user", "prod")
        self.assertEqual(json.loads(body), {"message": "hello, prod-user", "environment": "prod"})

    def test_openapi_is_served_from_file(self):
        status, _, body = route("/openapi.yaml", "", "dev")
        self.assertEqual(status, 200)
        self.assertEqual(body, OPENAPI.read_bytes())

    def test_unknown_path_is_404(self):
        self.assertEqual(route("/nope", "", "dev")[0], 404)

    def test_every_documented_path_is_served(self):
        # openapi.yaml の paths に書いた経路が、どれも 404 でないこと (書き足し忘れを落とす)
        paths = re.findall(r"^  (/\S*):$", OPENAPI.read_text(), re.M)
        self.assertTrue(paths)
        for p in paths:
            self.assertNotEqual(route(p, "", "dev")[0], 404, p)


if __name__ == "__main__":
    unittest.main()
