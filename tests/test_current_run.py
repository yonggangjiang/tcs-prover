"""Current-run attachment uses authenticated reads and never duplicates workers."""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from ui import current_run as cr
from ui import cli


class ProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.token = 'private_test_token_12345678901234567890'
        self.jobs = [{'runId': 'P01'}, {'runId': 'P02'}]
        self.requests = []
        self.status = 200
        super().__init__(('127.0.0.1', 0), ProbeHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.url = self.origin + '/#' + self.token


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        valid = (self.path == '/jobs' and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                 and self.headers.get('Origin') == self.server.origin
                 and self.headers.get('X-TCS-Prover-Token') == self.server.token)
        status = self.server.status if valid else 403
        body = json.dumps({'jobs': self.server.jobs}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if status == 302:
            self.send_header('Location', 'http://example.com/never-follow')
        self.end_headers()
        self.wfile.write(body)


class CurrentRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / 'physical-v4'
        self.runs.mkdir()
        for name in ('P01', 'P02'):
            (self.runs / name).mkdir()
        self.controller_path = self.root / 'controller.json'
        self.dashboard_path = self.root / 'dashboard.json'
        self.config_path = self.root / 'current-run.json'
        self.controller, self.dashboard = ProbeServer(), ProbeServer()
        for server in (self.controller, self.dashboard):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            self.addCleanup(self.close_server, server, thread)
        self.controller_record = {'controllerPid': os.getpid(), 'url': self.controller.url,
            'runs': [{'runId': name, 'phase': 'running', 'pid': os.getpid()} for name in ('P01', 'P02')]}
        self.dashboard_record = {'controllerPid': os.getpid(), 'url': self.dashboard.url,
                                 'legacyRuns': str(self.runs)}
        self.config = {'version': 1, 'runsRoot': str(self.runs),
                       'controllerStatus': str(self.controller_path), 'dashboardStatus': str(self.dashboard_path)}
        self.write_records()

    @staticmethod
    def close_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def write_records(self):
        for path, value in ((self.config_path, self.config), (self.controller_path, self.controller_record),
                            (self.dashboard_path, self.dashboard_record)):
            path.write_text(json.dumps(value))

    def select(self):
        return cr.select_current_dashboard(self.config_path)

    def unavailable(self):
        self.controller.status = self.dashboard.status = 503

    def idle_records(self):
        for row in self.controller_record['runs']:
            row.update(phase='done', pid=None)
        self.write_records()

    def test_prefers_authenticated_dashboard_and_keeps_extra_new_jobs(self):
        self.dashboard.jobs.append({'runId': 'new-independent-job'})
        before = {path: path.read_bytes() for path in (self.config_path, self.controller_path, self.dashboard_path)}
        selection = self.select()
        self.assertTrue(selection.attached)
        self.assertEqual(selection.url, self.dashboard.url)
        self.assertEqual(selection.runs_root, self.runs)
        self.assertEqual(self.dashboard.requests, ['/jobs'])
        self.assertEqual(self.controller.requests, [])
        self.assertNotIn(self.dashboard.token, repr(selection))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_unavailable_dashboard_falls_back_to_original_controller(self):
        self.dashboard.status = 503
        selection = self.select()
        self.assertEqual(selection.url, self.controller.url)
        self.assertEqual(self.controller.requests, ['/jobs'])

    def test_missing_dashboard_can_still_attach_to_controller(self):
        self.dashboard_path.unlink()
        self.assertEqual(self.select().url, self.controller.url)

    def test_wrong_dashboard_membership_or_runroot_uses_controller(self):
        self.dashboard.jobs = [{'runId': 'unrelated'}]
        self.assertEqual(self.select().url, self.controller.url)
        self.dashboard_record['legacyRuns'] = str(self.root / 'unrelated')
        self.write_records()
        self.dashboard.requests.clear()
        self.assertEqual(self.select().url, self.controller.url)
        self.assertEqual(self.dashboard.requests, [])

    def test_missing_config_preserves_normal_launcher_behavior(self):
        self.config_path.unlink()
        self.assertEqual(self.select(), cr.Selection())
        self.assertEqual(self.dashboard.requests + self.controller.requests, [])

    def test_alive_but_unreachable_controller_fails_without_secret(self):
        self.unavailable()
        with self.assertRaisesRegex(cr.CurrentRunError, 'No second controller') as caught:
            self.select()
        self.assertNotIn(self.dashboard.token, str(caught.exception))
        self.assertNotIn(self.controller.token, str(caught.exception))

    def test_dead_controllers_restore_only_explicitly_idle_selected_runs(self):
        self.unavailable()
        self.idle_records()
        with patch.object(cr, '_pid_alive', return_value=False):
            selection = self.select()
        self.assertFalse(selection.attached)
        self.assertEqual(selection.runs_root, self.runs)

    def test_dead_controllers_with_live_or_recorded_workers_refuse_recovery(self):
        self.unavailable()
        with patch.object(cr, '_pid_alive', return_value=False):
            with self.assertRaisesRegex(cr.CurrentRunError, 'unfinished or active work'):
                self.select()
            self.controller_record['runs'][0].update(phase='done', pid=os.getpid())
            self.controller_record['runs'][1].update(phase='paused', pid=None)
            self.write_records()
            with self.assertRaisesRegex(cr.CurrentRunError, 'unfinished or active work'):
                self.select()

    def test_stale_running_phase_refuses_recovery_even_without_worker_pid(self):
        self.unavailable()
        for row in self.controller_record['runs']:
            row['pid'] = None
        self.write_records()
        with patch.object(cr, '_pid_alive', return_value=False):
            with self.assertRaisesRegex(cr.CurrentRunError, 'unfinished or active work'):
                self.select()

    def test_unknown_dashboard_status_does_not_mean_both_controllers_dead(self):
        self.unavailable()
        self.idle_records()
        self.dashboard_path.unlink()
        with patch.object(cr, '_pid_alive', return_value=False):
            with self.assertRaisesRegex(cr.CurrentRunError, 'may still be running'):
                self.select()

    def test_pid_permission_error_is_live_and_never_kills_processes(self):
        with patch.object(cr.os, 'kill', side_effect=PermissionError) as kill:
            self.assertIs(cr._pid_alive(123), True)
            kill.assert_called_once_with(123, 0)
        with patch.object(cr.os, 'kill', side_effect=ProcessLookupError):
            self.assertIs(cr._pid_alive(123), False)

    def test_bad_config_and_nonloopback_status_never_probe(self):
        for value in ({**self.config, 'version': True}, {**self.config, 'runsRoot': 'relative'},
                      {**self.config, 'upstream': 'http://example.com'}):
            self.config_path.write_text(json.dumps(value))
            with self.assertRaises(cr.CurrentRunError):
                self.select()
        self.write_records()
        self.controller_record['url'] = 'https://example.com/#' + self.controller.token
        self.write_records()
        with self.assertRaises(cr.CurrentRunError):
            self.select()
        self.assertEqual(self.dashboard.requests + self.controller.requests, [])

    def test_redirects_auth_failure_and_duplicate_job_ids_are_not_accepted(self):
        self.controller.status = 503
        for status, jobs in ((302, self.dashboard.jobs), (403, self.dashboard.jobs),
                              (200, [{'runId': 'P01'}, {'runId': 'P01'}, {'runId': 'P02'}])):
            self.dashboard.status = status
            self.dashboard.jobs = jobs
            with self.assertRaises(cr.CurrentRunError):
                self.select()
        self.assertEqual(self.dashboard.requests, ['/jobs', '/jobs', '/jobs'])

    def cli(self, arguments, selection=None, error=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'argv', ['web_ui.py', *arguments]), \
                patch.object(cli, 'select_current_dashboard', side_effect=error, return_value=selection) as select, \
                patch.object(cli, 'Server') as server, patch.object(cli, 'App') as app, \
                patch.object(cli.webbrowser, 'open') as browser, redirect_stdout(output), redirect_stderr(errors):
            result = cli.main()
        return result, output.getvalue(), errors.getvalue(), select, server, app, browser

    def test_cli_attachment_never_constructs_server_or_app(self):
        selection = self.select()
        code, output, errors, select, server, app, browser = self.cli([], selection)
        self.assertEqual(code, 0)
        self.assertIn(selection.url, output)
        self.assertEqual(errors, '')
        select.assert_called_once_with()
        server.assert_not_called()
        app.assert_not_called()
        browser.assert_called_once_with(selection.url)

    def test_cli_no_browser_prints_only_existing_url(self):
        selection = self.select()
        code, output, _, _, server, app, browser = self.cli(['--no-browser'], selection)
        self.assertEqual(code, 0)
        self.assertEqual(output, selection.url + '\n')
        server.assert_not_called()
        app.assert_not_called()
        browser.assert_not_called()

    def test_cli_unreachable_controller_returns_error_without_restore(self):
        code, _, errors, _, server, app, browser = self.cli([], error=cr.CurrentRunError('No second controller was started.'))
        self.assertEqual(code, 1)
        self.assertIn('No second controller', errors)
        server.assert_not_called()
        app.assert_not_called()
        browser.assert_not_called()

    def test_cli_recovery_uses_configured_physical_runs_not_default(self):
        fake = Mock(origin='http://127.0.0.1:1234', token='test')
        fake.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(cli.sys, 'argv', ['web_ui.py', '--no-browser']), \
                patch.object(cli, 'select_current_dashboard', return_value=cr.Selection(self.runs)), \
                patch.object(cli, 'Server', return_value=fake) as server, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        server.assert_called_once_with((cli.HOST, cli.PORT), runs=self.runs, single_instance=True)


if __name__ == '__main__':
    unittest.main()
