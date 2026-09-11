"""The author stays a single generic goal node with YAML-defined research records."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow_runner as runtime


class AuthorWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.workflow = copy.deepcopy(runtime.builtin_workflow('author_critic'))
        author = self.workflow['nodes']['author']
        author['next']['done'] = 'end'
        self.workflow['nodes'] = {'author': author}
        self.calls = []

    def goal(self, runtime, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        yield {'outcome': 'done', 'output': 'Complete candidate'}

    def execute(self, options=None):
        options = options or {}
        with patch.object(runtime, 'goal_session', side_effect=self.goal), patch.object(runtime, 'structured') as structured, patch.object(runtime, 'emit'):
            result = runtime._execute(self.workflow, {'statement': 'Exact statement'}, options, self.workflow['prompts'])
        structured.assert_not_called()
        return result

    def test_author_node_does_not_create_or_manage_files(self):
        result = self.execute()
        self.assertEqual(result['solution'], 'Complete candidate')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.directory.iterdir()), [])
        prompt = self.calls[0][0]
        self.assertIn('Exact statement', prompt)
        self.assertNotIn('[STATEMENT]', prompt)

    def test_yaml_controls_goal_names_and_prompts(self):
        node = self.workflow['nodes'].pop('author')
        self.workflow['nodes']['thinker'] = node
        node['marker'] = '<TASK>'
        self.workflow['prompts']['author'] = 'Custom instructions for <TASK>'
        self.execute()
        prompt, call = self.calls[0]
        self.assertEqual(prompt, 'Custom instructions for Exact statement')
        self.assertEqual(call['node_name'], 'thinker')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_resume_passes_workspace_and_thread_without_reading_notebooks(self):
        path = self.directory / 'arbitrary-private-file.txt'
        path.write_bytes(b'User-managed contents\r\n')
        options = {'goal_cwd': str(self.directory), 'goal_resume': True, 'goal_thread_id': 'saved-thread'}
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('Runner read a notebook')), patch.object(Path, 'read_text', side_effect=AssertionError('Runner read a notebook')):
            self.execute(options)
        self.assertEqual(path.read_bytes(), b'User-managed contents\r\n')
        self.assertEqual(self.calls[0][1]['options']['goal_cwd'], str(self.directory))
        self.assertEqual(self.calls[0][1]['options']['goal_thread_id'], 'saved-thread')


if __name__ == '__main__':
    unittest.main()
