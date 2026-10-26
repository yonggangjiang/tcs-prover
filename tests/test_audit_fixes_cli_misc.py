"""Regressions for the launcher, terminal CLI, saved-batch and gateway fixes.

No test starts a model, a Codex process or a third-party network request.
"""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import web_ui
import workflow_runner as runtime
from ui import batches, cli, legacy_overview, live_gateway, review, retrieval, server
from ui.current_run import Selection


class WindowsLauncherTests(unittest.TestCase):
    """Windows execv would split spaced arguments and exit 0 at once."""

    def launch(self, wait=None, popen_error=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        executable = root / '.venv' / 'Scripts' / 'python.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        child = mock.Mock()
        child.wait.side_effect = wait
        replace = mock.Mock(side_effect=AssertionError('execv cannot keep arguments on Windows'))
        # Replace only the launcher's os reference: pathlib must stay POSIX here.
        windows = SimpleNamespace(name='nt', execv=replace)
        arguments = ['web_ui.py', '--no-browser', 'my statement.md']
        errors = io.StringIO()
        with mock.patch.object(web_ui, '__file__', str(root / 'web_ui.py')), \
                mock.patch.object(web_ui, 'os', windows), \
                mock.patch.object(web_ui.sys, 'argv', arguments), \
                mock.patch('subprocess.Popen', return_value=child, side_effect=popen_error) as popen, \
                contextlib.redirect_stderr(errors):
            code = web_ui.main()
        flags = ['-B'] if sys.dont_write_bytecode else []
        command = [str(executable), *flags, str(root / 'web_ui.py'), '--no-browser', 'my statement.md']
        return code, popen, child, replace, command, errors.getvalue()

    def test_windows_child_keeps_arguments_and_returns_its_status(self):
        code, popen, child, replace, command, _ = self.launch(wait=[7])
        self.assertEqual(code, 7)
        # A list lets subprocess quote 'my statement.md' as one argument.
        popen.assert_called_once_with(command)
        replace.assert_not_called()
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_windows_ctrl_c_waits_for_child_cleanup_and_status(self):
        code, _, child, _, _, _ = self.launch(wait=[KeyboardInterrupt, KeyboardInterrupt, 130])
        self.assertEqual(code, 130)
        self.assertEqual(child.wait.call_count, 3)
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_windows_start_failure_is_reported(self):
        code, _, _, _, _, errors = self.launch(popen_error=OSError('missing interpreter'))
        self.assertEqual(code, 1)
        self.assertIn('Cannot start the project Python environment', errors)


class HeadlessCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / 'runs'
        self.statement = self.root / 'problem.md'
        self.statement.write_text('Every finite graph has property P.\n', encoding='utf-8')

    def run_headless(self, start):
        output, errors = io.StringIO(), io.StringIO()
        # A full snapshot re-reads the editable workflow YAML; polling must not.
        with mock.patch.object(cli, 'direct_cli_options', return_value={}), \
                mock.patch.object(server.App, 'start_direct_statement', autospec=True, side_effect=start), \
                mock.patch.object(server.App, 'snapshot', side_effect=AssertionError('full snapshot')), \
                mock.patch.object(server.App, '_latest_trace',
                                  side_effect=AssertionError('parsed another job transcript')):
            code = cli.run_headless_markdown(self.statement, runs=self.runs,
                                             output_stream=output, error_stream=errors)
        return code, errors.getvalue()

    def test_wait_loop_keeps_pausing_job_until_its_pause_is_saved(self):
        finished = threading.Event()

        def start(app, statement, **options):
            app.run_dir = self.runs / 'job'
            app.state.update(phase='pausing', error='')

            def settle():
                time.sleep(0.4)
                with app.lock:
                    app.state.update(phase='paused', error='Workflow time limit reached.')
                    finished.set()

            threading.Thread(target=settle, daemon=True).start()

        code, errors = self.run_headless(start)
        self.assertTrue(finished.is_set())
        self.assertEqual(code, 1)
        self.assertIn('[problem.md] Proof failed: Workflow time limit reached.', errors)

    def test_job_stays_active_while_its_reader_saves_artifacts(self):
        app = server.App(runs=self.runs)
        for phase in ('reviewing', 'running', 'stopping', 'pausing'):
            app.state['phase'] = phase
            self.assertTrue(cli._job_active(app), phase)
        app.state['phase'] = 'paused'
        self.assertFalse(cli._job_active(app))
        app.worker_token = object()
        self.assertTrue(cli._job_active(app))

    def test_finished_job_reports_success_without_snapshot(self):
        def start(app, statement, **options):
            app.run_dir = self.runs / 'job'
            app.state.update(phase='done', error='')

        code, errors = self.run_headless(start)
        self.assertEqual(code, 0)
        self.assertIn(f'[problem.md] Proof finished in {self.runs / "job"}', errors)

    def test_concise_output_names_repair_turns_and_reports_author_pauses(self):
        stream = io.StringIO()
        output = cli.ConciseHeadlessOutput(stream, threading.Lock(), 'a.md')
        for record in ({'kind': 'request', 'stage': 'solve', 'label': 'Exact solve input'},
                       {'kind': 'request', 'stage': 'critic', 'label': 'Critic input'},
                       {'kind': 'request', 'stage': 'repair', 'label': 'Exact solve input'},
                       {'kind': 'status', 'stage': 'repair', 'label': 'Author paused',
                        'text': 'Workflow time limit reached.'}):
            output.write(json.dumps(record) + '\n')
        self.assertEqual(stream.getvalue().splitlines(), [
            '[a.md] Current step: Proof author',
            '[a.md] Current step: Independent critic (round 1)',
            '[a.md] Current step: Proof author (repairing critic objections)',
            '[a.md] Author paused: Workflow time limit reached.',
        ])


class CliMainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'saved-run'
        self.source.mkdir()

    def resume(self, *options):
        manager = mock.Mock()
        manager.start_saved_research_job.return_value.state = {'runId': 'continued'}
        manager.serve_forever.side_effect = KeyboardInterrupt
        arguments = ['web_ui.py', '--resume-research', str(self.source), '--no-browser', *options]
        with mock.patch.object(cli.sys, 'argv', arguments), \
                mock.patch.object(cli, 'saved_research_source', return_value={'run_dir': self.source}), \
                mock.patch.object(cli, 'Server', return_value=manager), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        manager.start_saved_critic_job.assert_not_called()
        return manager.start_saved_research_job.call_args.args[1]

    def test_resume_author_applies_abbreviated_overrides(self):
        model = next(name for name in cli.OFFERED_MODELS if name != cli.DEFAULT_AUTHOR_MODEL)
        self.assertEqual(self.resume('-thinking', '24', '--author-m', model),
                         {'thinkingHours': 24.0, 'authorModel': model})
        self.assertEqual(self.resume('-speed', 'fast', '-authorE', 'low', '--critic-r', '5'),
                         {'speedMode': 'fast', 'authorEffort': 'low', 'criticRounds': 5})

    def test_resume_author_keeps_saved_settings_without_overrides(self):
        self.assertEqual(self.resume(), {})
        # An explicit value equal to the default is still an explicit override.
        self.assertEqual(self.resume('--thinking-hours', str(cli.DEFAULT_THINKING_HOURS)),
                         {'thinkingHours': float(cli.DEFAULT_THINKING_HOURS)})

    def test_ready_url_is_flushed_before_the_server_blocks(self):
        class Recording(io.StringIO):
            flushed = ''

            def flush(self):
                self.flushed = self.getvalue()
                super().flush()

        stream, seen = Recording(), []
        manager = mock.Mock(origin='http://127.0.0.1:8765', token='private-session-token')

        def serve():
            seen.append(stream.flushed)
            raise KeyboardInterrupt

        manager.serve_forever.side_effect = serve
        with mock.patch.object(cli.sys, 'argv', ['web_ui.py', '--no-browser']), \
                mock.patch.object(cli, 'select_current_dashboard', return_value=Selection()), \
                mock.patch.object(cli, 'Server', return_value=manager), \
                mock.patch.object(cli.sys, 'stdout', stream):
            self.assertEqual(cli.main(), 0)
        self.assertIn('TCS Prover is ready at http://127.0.0.1:8765/#private-session-token', seen[0])

    def test_port_in_use_reports_the_existing_message(self):
        errors = io.StringIO()
        with mock.patch.object(cli.sys, 'argv', ['web_ui.py', '--no-browser']), \
                mock.patch.object(cli, 'select_current_dashboard', return_value=Selection()), \
                mock.patch.object(cli, 'Server', side_effect=OSError(errno.EADDRINUSE, 'in use')), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(), 1)
        self.assertIn('is already in use', errors.getvalue())


class SavedBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'runs'
        self.root.mkdir()
        self.batch = self.root / 'previous'
        self.batch.mkdir()
        self.run = self.make_run(self.batch, 'job')
        self.catalog = batches.BatchCatalog(self.root, current_root=self.root / 'current')

    @staticmethod
    def make_run(root, name, **settings):
        directory = root / name
        directory.mkdir()
        (directory / 'draft.md').write_text('# Draft problem\n\nA finite theorem.\n')
        (directory / 'job-settings.json').write_text(json.dumps(settings))
        return directory

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value) if not isinstance(value, str) else value)

    def transcript(self, *times):
        self.write('transcript.jsonl', ''.join(
            json.dumps({'kind': 'status', 'stage': 'solve', 'time': f'2026-01-01T{time}:00Z'}) + '\n'
            for time in times))
        # Lists read only file times, so align them with the records.
        for name, time in (('draft.md', times[0]), ('transcript.jsonl', times[-1])):
            stamp = batches._timestamp(f'2026-01-01T{time}:00Z').timestamp()
            os.utime(self.run / name, (stamp, stamp))

    def elapsed(self):
        return (self.catalog.jobs('previous')[0]['activeElapsedSeconds'],
                self.catalog.state('previous', 'job')['activeElapsedSeconds'])

    def test_finished_job_that_never_paused_counts_its_whole_run(self):
        self.write('final.tex', 'Complete proof.')
        self.transcript('00:00', '05:00', '10:00')
        self.assertEqual(self.elapsed(), (36000, 36000))

    def test_running_checkpoint_adds_the_last_segment(self):
        self.write('pause.json', {'status': 'running', 'elapsedSeconds': 3600})
        self.write('job-settings.json', {'resumedAt': '2026-01-01T05:00:00Z'})
        self.transcript('00:00', '10:00')
        self.assertEqual(self.elapsed(), (21600, 21600))

    def test_pausing_checkpoint_beside_a_result_is_not_counted_twice(self):
        self.write('final.tex', 'Complete proof.')
        self.write('pause.json', {'status': 'pausing', 'elapsedSeconds': 50000})
        self.write('job-settings.json', {'elapsedSeconds': 3600, 'resumedAt': '2026-01-01T05:00:00Z'})
        self.transcript('00:00', '10:00')
        self.assertEqual(self.elapsed(), (21600, 21600))

    def test_paused_and_prepared_jobs_keep_frozen_totals(self):
        self.write('pause.json', {'status': 'paused', 'elapsedSeconds': 120})
        self.write('job-settings.json', {'elapsedSeconds': 60, 'resumedAt': '2026-01-01T05:00:00Z'})
        self.transcript('00:00', '10:00')
        self.assertEqual(self.elapsed(), (120, 120))
        (self.run / 'pause.json').unlink()
        (self.run / 'transcript.jsonl').unlink()
        self.write('job-settings.json', {'preparedRun': True, 'elapsedSeconds': 60})
        self.assertEqual(self.elapsed(), (0, 0))

    def test_trash_folder_is_not_a_batch_or_run(self):
        trash = self.root / '.trash'
        trash.mkdir()
        self.make_run(trash, 'deleted-job')
        self.make_run(self.root, '.hidden-job')
        self.make_run(self.root, 'visible-job')
        self.assertEqual({card['id'] for card in self.catalog.list_batches()}, {'__root__', 'previous'})
        self.assertEqual([job['runId'] for job in self.catalog.jobs('__root__')], ['visible-job'])
        for call in (lambda: self.catalog.jobs('.trash'),
                     lambda: self.catalog.state('.trash', 'deleted-job'),
                     lambda: self.catalog.artifact('.trash', 'deleted-job', 'transcript.jsonl'),
                     lambda: self.catalog.state('__root__', '.hidden-job')):
            with self.assertRaises(ValueError):
                call()

    def test_truncation_notice_points_to_the_saved_transcript(self):
        path = self.run / 'transcript.jsonl'
        with path.open('wb') as stream:
            stream.write(b'{"kind":"status"}\n')
            stream.seek(batches.MAX_TRACE_BYTES + 1024)
            stream.write(b'\n{"kind":"status","stage":"solve"}\n')
        notice = self.catalog.state('previous', 'job')['traceNotice']
        self.assertIn('complete history', notice)
        self.assertIn('transcript.jsonl', notice)
        self.assertIn('transcript/view_transcript.py', notice)
        self.assertNotIn('Download', notice)

    def test_lists_use_the_display_policy_memo(self):
        cutoff = '2026-01-01T00:00:00Z'
        policy_path = self.root.parent / 'controller' / 'policy.json'
        self.write('job-settings.json', {'retrievalPolicyPath': str(policy_path)})
        policy = {'isolation': {'workspace': str(self.run)}, 'cutoffExclusiveUtc': cutoff}
        with mock.patch.object(retrieval, 'display_policy', return_value=policy) as display, \
                mock.patch.object(retrieval.retrieval_runtime, 'load_policy',
                                  side_effect=AssertionError('rehashed the Codex binary')):
            job = self.catalog.jobs('previous')[0]
        display.assert_called_once_with(policy_path)
        self.assertEqual((job['retrievalMode'], job['retrievalCutoffUtc']), ('historical', cutoff))

    def test_unloadable_policy_keeps_the_saved_cutoff(self):
        cutoff = '2026-01-01T00:00:00Z'
        self.write('job-settings.json', {'retrievalPolicyPath': str(self.root.parent / 'missing.json'),
                                         'retrievalMode': 'historical', 'retrievalCutoffUtc': cutoff})
        state = self.catalog.state('previous', 'job')
        self.assertEqual(state['retrievalCutoffUtc'], cutoff)
        self.assertEqual(state['retrieval']['cutoffExclusiveUtc'], cutoff)
        self.assertFalse(state['retrieval']['available'])
        self.assertEqual(self.catalog.jobs('previous')[0]['retrievalCutoffUtc'], cutoff)


class LegacyOverviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'problem-01'
        self.run.mkdir()
        self.transcript = self.run / 'transcript.jsonl'
        self.state = {'runId': self.run.name, 'phase': 'done', 'startedAt': '2026-10-16T10:00:00Z'}
        self.status = self.root / 'status.json'
        self.status.write_text('{}')

    def augment(self, **changes):
        return legacy_overview.augment_legacy(
            {**self.state, **changes}, runs_root=self.root, status_path=self.status,
            retrieval_loader=lambda path: {'mode': 'live'})

    @staticmethod
    def line(record):
        return json.dumps(record) + '\n'

    def test_polls_parse_only_lines_appended_since_the_last_poll(self):
        approval = {'kind': 'status', 'label': 'Critic approved', 'text': 'Round 1 passed.'}
        self.transcript.write_text(self.line(approval) + self.line({'kind': 'status'}) * 50)
        self.assertEqual(self.augment()['criticAcceptances'], [approval])
        with self.transcript.open('a') as stream:
            stream.write(self.line({'kind': 'final_result', 'output': 'Complete proof'}))
        with mock.patch.object(legacy_overview, '_fold', wraps=legacy_overview._fold) as folded:
            result = self.augment()
        self.assertEqual(folded.call_count, 1)
        self.assertEqual(result['resultKind'], 'final')
        self.assertEqual(result['criticAcceptances'], [approval])

    def test_unfinished_last_line_counts_once_after_it_is_completed(self):
        approval = {'kind': 'status', 'label': 'Critic approved', 'text': 'Round 1 passed.'}
        self.transcript.write_text(self.line({'kind': 'status'}) + json.dumps(approval))
        self.assertEqual(self.augment()['criticAcceptances'], [approval])
        with self.transcript.open('a') as stream:
            stream.write('\n' + self.line({'kind': 'status'}))
        self.assertEqual(self.augment()['criticAcceptances'], [approval])

    def test_shrunk_or_rewritten_transcript_is_rescanned(self):
        first = {'kind': 'status', 'label': 'Critic approved', 'text': 'First'}
        self.transcript.write_text(self.line(first) * 3)
        self.assertEqual(len(self.augment()['criticAcceptances']), 3)
        self.transcript.write_text(self.line(first))
        self.assertEqual(self.augment()['criticAcceptances'], [first])
        second = {**first, 'text': 'Second'}
        self.transcript.write_text(self.line(second) + self.line({'kind': 'status', 'label': 'x' * 40}))
        self.assertEqual(self.augment()['criticAcceptances'], [second])

    def test_unloadable_policy_keeps_the_saved_cutoff(self):
        cutoff = '2026-01-01T00:00:00Z'
        source = {**self.state, 'retrievalMode': 'historical', 'retrievalCutoffUtc': cutoff,
                  'retrievalPolicyPath': str(self.root / 'missing-policy.json')}
        result = legacy_overview.augment_legacy(source, runs_root=self.root, status_path=self.status)
        self.assertEqual(result['retrievalCutoffUtc'], cutoff)
        self.assertEqual(result['retrieval']['cutoffExclusiveUtc'], cutoff)
        self.assertFalse(result['retrieval']['available'])


class GatewayStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'gateway.json'

    def test_status_record_is_private_atomic_and_needs_no_fchmod(self):
        self.path.write_text('{"url": "previous"}')
        self.path.chmod(0o644)
        record = {'url': 'http://127.0.0.1:8772/#token', 'controllerPid': 1}
        with mock.patch.object(live_gateway.os, 'fchmod', create=True,
                               side_effect=AttributeError('os.fchmod is unavailable on Windows')):
            live_gateway.write_launch_status(self.path, record)
        self.assertEqual(json.loads(self.path.read_text()), record)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ['gateway.json'])

    def test_failed_write_keeps_previous_record(self):
        self.path.write_text('{"url": "previous"}')
        with self.assertRaises(TypeError):
            live_gateway.write_launch_status(self.path, {'url': object()})
        self.assertEqual(self.path.read_text(), '{"url": "previous"}')
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ['gateway.json'])

    def gateway(self, port=0):
        return live_gateway.GatewayServer(
            ('127.0.0.1', port), legacy=live_gateway.Upstream(1, 'legacy-token'),
            current=live_gateway.Upstream(2, 'current-token'), augment_legacy=lambda value, **_: value)

    def test_gateway_never_shares_a_listening_port(self):
        self.assertEqual(live_gateway.GatewayServer.allow_reuse_address, os.name != 'nt')
        first = self.gateway()
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError) as caught:
            self.gateway(first.server_port).server_close()
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)


class ReviewTests(unittest.TestCase):
    def review(self, statement, notes):
        with mock.patch.object(review.runtime, 'structured',
                               return_value=({'statement': statement, 'notes': notes}, 'raw')), \
                mock.patch.object(review.runtime, 'emit') as emit:
            try:
                return review.review('A user draft.')
            finally:
                self.emitted = emit.call_args_list

    def test_empty_statement_reports_reviewer_notes_on_one_line(self):
        with self.assertRaises(runtime.Error) as caught:
            self.review('  \n', 'The claim is false because\n\nn = 3 is a counterexample.\n')
        self.assertEqual(str(caught.exception), 'The reviewer returned an empty statement. '
                         'Reviewer notes: The claim is false because n = 3 is a counterexample.')
        self.assertNotIn('problem statement is empty', str(caught.exception))
        # A review result would let the controller accept the empty review.
        self.assertEqual(self.emitted, [])

    def test_empty_statement_without_notes_and_long_notes(self):
        with self.assertRaises(runtime.Error) as caught:
            self.review('', ' ')
        self.assertEqual(str(caught.exception),
                         'The reviewer returned an empty statement; see the activity log for its output.')
        with self.assertRaises(runtime.Error) as caught:
            self.review('', 'x' * 1500)
        self.assertTrue(str(caught.exception).endswith('x' * 1000 + '...'))

    def test_nonempty_review_is_unchanged(self):
        self.assertEqual(self.review('  A statement.  ', ' Notes. '),
                         {'statement': 'A statement.', 'notes': 'Notes.'})
        self.assertEqual(len(self.emitted), 1)


if __name__ == '__main__':
    unittest.main()
