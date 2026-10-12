"""api-auth's check, without a cluster: python3 -m unittest discover -s components/api-auth/tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import server  # noqa: E402

STORE = {"api-whoami": {"api-key": "k1", "api-key-ci": "k2", "note": "not-a-key"},
         "api-empty": {"api-key": ""}}


def keys():
    calls = []

    def fetch(ns, name):
        calls.append((ns, name))
        return STORE.get(name)
    return server.Keys(fetch), calls


class Check(unittest.TestCase):
    def check(self, uri, **headers):
        return server.check(keys()[0], uri, headers)[0]

    def test_api_name(self):
        self.assertEqual(server.api_name("/usage/v1/events?x=1"), "usage")
        self.assertEqual(server.api_name("/whoami"), "whoami")
        self.assertIsNone(server.api_name("/"))
        self.assertIsNone(server.api_name(None))
        self.assertIsNone(server.api_name("/../secret"))
        self.assertIsNone(server.api_name("/Bad_Name/x"))

    def test_bearer_and_header(self):
        self.assertEqual(self.check("/whoami/x", Authorization="Bearer k1"), 200)
        self.assertEqual(self.check("/whoami/x", Authorization="bearer k2"), 200)
        self.assertEqual(self.check("/whoami", **{"X-API-Key": "k2"}), 200)

    def test_denied(self):
        self.assertEqual(self.check("/whoami/x"), 401)                                   # no key
        self.assertEqual(self.check("/whoami/x", Authorization="Bearer nope"), 401)      # wrong key
        self.assertEqual(self.check("/whoami/x", Authorization="Bearer not-a-key"), 401) # not an api-key* field
        self.assertEqual(self.check("/whoami/x", Authorization="Basic azE="), 401)       # not a bearer token

    def test_fails_closed(self):
        self.assertEqual(self.check("/other/x", Authorization="Bearer k1"), 401)   # no entry api-other
        self.assertEqual(self.check("/empty/x", Authorization="Bearer "), 401)     # empty keys never match
        self.assertEqual(self.check("/", Authorization="Bearer k1"), 401)          # no API in the path

    def test_another_apis_key_doesnt_work(self):
        STORE["api-usage"] = {"api-key": "u1"}
        try:
            self.assertEqual(self.check("/whoami/x", Authorization="Bearer u1"), 401)
            self.assertEqual(self.check("/usage/x", Authorization="Bearer u1"), 200)
        finally:
            del STORE["api-usage"]

    def test_cached(self):
        k, calls = keys()
        server.check(k, "/whoami", {"Authorization": "Bearer k1"})
        server.check(k, "/whoami", {"Authorization": "Bearer k1"})
        self.assertEqual(calls, [("secret-store", "api-whoami")])


if __name__ == "__main__":
    unittest.main()
