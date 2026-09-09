"""The author stays a single generic goal node with three YAML-defined files."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goal_runtime import _seed_files
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
        author['next']['proof'] = 'end'
        self.workflow['nodes'] = {'author': author}
        self.calls = []

    def goal(self, runtime, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        _seed_files(runtime, kwargs['directory'], kwargs['files'], kwargs['prompt_file'])
        yield {'outcome': 'proof', 'solution': 'Complete candidate'}

    def execute(self, options=None):
        options = options or {}
        with patch('goal_runtime.goal_session', side_effect=self.goal), patch.object(runtime, 'structured') as structured, patch.object(runtime, 'emit'):
            result = runtime._execute(self.workflow, {'statement': 'Exact statement'}, options, self.workflow['prompts'])
        structured.assert_not_called()
        return result

    def test_one_author_session_creates_only_three_notebooks(self):
        result = self.execute()
        self.assertEqual(result['solution'], 'Complete candidate')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({p.name for p in self.directory.iterdir()},
                         {'INITIAL_PROMPT.md', 'APPROACHES.md', 'PROVED.md'})
        prompt = self.calls[0][0]
        self.assertIn('Exact statement', prompt)
        self.assertNotIn('[STATEMENT]', prompt)
        self.assertEqual((self.directory / 'INITIAL_PROMPT.md').read_text(), prompt)

    def test_yaml_controls_goal_names_files_and_prompts(self):
        node = self.workflow['nodes'].pop('author')
        self.workflow['nodes']['thinker'] = node
        node['prompt_file'] = 'assignment.txt'
        node['files'] = {'assignment.txt': '{original_prompt}', 'history.txt': 'Past work', 'facts.txt': 'Facts'}
        node['marker'] = '<TASK>'
        self.workflow['prompts']['author'] = 'Custom instructions for <TASK>'
        self.execute()
        prompt, call = self.calls[0]
        self.assertEqual(prompt, 'Custom instructions for Exact statement')
        self.assertEqual(call['node_name'], 'thinker')
        self.assertEqual({p.name for p in self.directory.iterdir()}, set(node['files']))

    def test_resume_uses_exact_initial_file_without_newline_normalization(self):
        original = b'Original instructions for Exact statement.\r\nKeep this exact text.\r\n'
        path = self.directory / 'INITIAL_PROMPT.md'
        path.write_bytes(original)
        (self.directory / 'APPROACHES.md').write_text('A001 CLOSED: the detailed failed argument.')
        (self.directory / 'PROVED.md').write_text('L001: a proved obstruction to A001.')
        options = {'author_input_file': str(path), 'goal_thread_id': 'saved-thread'}
        self.execute(options)
        self.assertEqual(self.calls[0][0].encode('utf-8'), original)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn('detailed failed argument', (self.directory / 'APPROACHES.md').read_text())
        self.assertEqual(self.calls[0][1]['options']['goal_thread_id'], 'saved-thread')


if __name__ == '__main__':
    unittest.main()
