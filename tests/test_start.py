"""Tests of the start-up: finding a running instance and taking over from another version."""
import contextlib
import io
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from dynamicswar import __version__, app, demo, server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_app(port):
    """A running DynamicsWar as app.main keeps it: it ends as soon as it is asked to quit."""
    state = server.State(cfg={}, save=lambda cfg: None, fetch=demo.DemoQobuz(delay=0), demo=True)
    httpd, url = server.serve(state, port)

    def loop():
        while not state.quit:
            time.sleep(0.05)
        httpd.shutdown()
        httpd.server_close()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return state, url, thread


class OldVersion:
    """Stands in for a version up to 0.7.0: it knows no /api/quit and stays while its window is open."""

    def __init__(self, port, version="0.6.0"):
        asked = self.asked = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    return self.answer(200, b'<title>DynamicsWar</title>\n<meta name="session" content="OLD">',
                                       "text/html")
                if self.path == "/api/status" and self.headers.get("X-Session") == "OLD":
                    return self.answer(200, json.dumps({"version": version}).encode())
                self.answer(403, b"{}")

            def do_POST(self):
                asked.append(self.path)
                self.answer(200 if self.path == "/api/bye" else 404, b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RunningInstanceTests(unittest.TestCase):
    def test_nothing_running(self):
        self.assertIsNone(app.running_instance(free_port()))

    def test_finds_address_session_and_version(self):
        port = free_port()
        state, url, thread = serve_app(port)
        try:
            self.assertEqual(app.running_instance(port), (url, state.session))
            self.assertEqual(app.instance_version(url, state.session), __version__)
            self.assertIsNone(app.instance_version(url, "wrong session"))
        finally:
            state.quit = True
            thread.join(5)

    def test_other_program_on_the_port_is_not_taken_for_the_app(self):
        port = free_port()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"hello")

        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertIsNone(app.running_instance(port))
        finally:
            httpd.shutdown()
            httpd.server_close()


class TakeOverTests(unittest.TestCase):
    def test_quit_needs_the_session(self):
        port = free_port()
        state, url, thread = serve_app(port)
        try:
            self.assertEqual(app.ask_instance(url, "/api/quit", "wrong session", post=True)[0], 403)
            self.assertFalse(state.quit)
            self.assertEqual(app.ask_instance(url, "/api/quit", state.session, post=True)[0], 200)
            self.assertTrue(state.quit)
        finally:
            state.quit = True
            thread.join(5)

    def test_running_instance_gives_way(self):
        port = free_port()
        state, url, thread = serve_app(port)
        self.assertTrue(app.replace_instance(url, state.session, port, wait=5))
        thread.join(5)
        self.assertIsNone(app.running_instance(port))

    def test_version_without_quit_is_told_goodbye_and_reported_when_it_stays(self):
        port = free_port()
        old = OldVersion(port)
        try:
            self.assertFalse(app.replace_instance(f"http://127.0.0.1:{port}/", "OLD", port, wait=1))
            self.assertEqual(old.asked, ["/api/quit", "/api/bye"])
        finally:
            old.close()


class MainTests(unittest.TestCase):
    def main(self, port):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(app, "alert") as alert:
            code = app.main(["--port", str(port), "--no-window"])
        return code, out.getvalue(), alert

    def test_same_version_shows_the_running_one(self):
        port = free_port()
        state, url, thread = serve_app(port)
        try:
            code, out, alert = self.main(port)
            self.assertEqual((code, out.strip()), (0, url))
            alert.assert_not_called()
            self.assertFalse(state.quit)
        finally:
            state.quit = True
            thread.join(5)

    def test_old_version_with_open_window_is_named(self):
        port = free_port()
        old = OldVersion(port)
        try:
            with mock.patch.object(app, "REPLACE_WAIT", 1):
                code, out, alert = self.main(port)
            self.assertEqual(code, 1)
            self.assertIn("DynamicsWar 0.6.0 is still open", alert.call_args[0][0])
            self.assertIn(__version__, alert.call_args[0][0])
        finally:
            old.close()

    def test_other_version_is_replaced_by_this_start(self):
        port = free_port()
        with mock.patch.object(server, "__version__", "0.0.1"):
            old_state, url, old_thread = serve_app(port)
            self.assertEqual(app.instance_version(url, old_state.session), "0.0.1")
            result = {}

            def start():
                result["main"] = self.main(port)

            # the patch only matters for the answer of the old instance; it must hold until that is read
            starter = threading.Thread(target=start, daemon=True)
            starter.start()
            old_thread.join(8)
        self.assertTrue(old_state.quit)                         # the old one was asked to end ...
        new = None
        for _ in range(50):                                     # ... and this start runs in its place
            new = app.running_instance(port)
            if new and new[1] != old_state.session:
                break
            time.sleep(0.1)
        self.assertIsNotNone(new)
        self.assertNotEqual(new[1], old_state.session)
        self.assertEqual(app.instance_version(*new), __version__)
        self.assertEqual(app.ask_instance(new[0], "/api/quit", new[1], post=True)[0], 200)
        starter.join(8)
        self.assertEqual(result["main"][0], 0)
        result["main"][2].assert_not_called()
        self.assertIsNone(app.running_instance(port))


if __name__ == "__main__":
    unittest.main()
