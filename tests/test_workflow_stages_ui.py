"""One input with independently selectable workflow stages."""
import itertools
import json
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from tests import test_provider_ui as browser


class WorkflowStageMarkupTests(unittest.TestCase):
    def test_one_input_and_four_enabled_stages_in_order(self):
        parsed = browser.page()
        markup = browser.INDEX.read_text(encoding='utf-8')
        identities = ['workflowReview', 'workflowAuthor', 'workflowCritic', 'workflowWriting']
        positions = [markup.index(f'id="{identity}"') for identity in identities]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(markup.index('id="problem"'), positions[0])
        for identity in identities:
            self.assertEqual(parsed.elements[identity]['type'], 'checkbox')
            self.assertIn('checked', parsed.elements[identity])
        for identity in ['latexInput', 'skipStatementReview', 'statementReviewOnly']:
            self.assertNotIn(identity, parsed.elements)
        for old in ['LaTeX polish', 'Skip statement review', 'Statement review only']:
            self.assertNotIn(old, markup)


class WorkflowStageBrowserTests(unittest.TestCase):
    run_app = browser.ProviderBrowserTests.run_app
    STAGES = [['function selectedWorkflowStages()', 'function selectedRetrievalMode()']]
    SETUP = r"""
const ui = new Proxy({}, {get:(target,key) => target[key] ||= new Element()});
let state = {};
let activePrompt = 'review';
const show = (element,visible) => {element.hidden=!visible;};
const updateModelSummary = () => {}, renderRetrievalSettings = () => {};
const selectPrompt = (name) => { activePrompt = name; };
"""

    def test_every_stage_subset_controls_only_its_settings_and_all_off_cannot_start(self):
        combinations = [dict(zip(['review','author','critic','writing'], flags))
                        for flags in itertools.product([False, True], repeat=4)]
        self.run_app(self.STAGES, self.SETUP, 'const combinations = '+json.dumps(combinations)+r""";
for (const stages of combinations) {
  setWorkflowStages(stages);
  assert.deepEqual(selectedWorkflowStages(), stages);
  for (const [stage,role] of [['review','review'],['author','author'],['critic','critic'],['writing','writer']]) {
    assert.equal(ui[role+'ModelSetting'].hidden,!stages[stage]);
  }
  assert.equal(ui.check.disabled,!Object.values(stages).some(Boolean));
  assert.equal(ui.criticRoundSetting.hidden,!stages.critic);
  assert.equal(ui.criticCountSetting.hidden,!stages.critic);
  assert.equal(ui.thinkingHoursSetting.hidden,!stages.author);
  assert.equal(ui.problemLabel.textContent,!stages.author&&(stages.critic||stages.writing)
    ? 'Statement and existing work':'Problem statement');
}
""")

    def test_stage_restore_prefers_saved_choices_and_reads_legacy_jobs(self):
        self.run_app(self.STAGES, self.SETUP, r"""
restoreWorkflowStages({});
assert.deepEqual(selectedWorkflowStages(), {review:true,author:true,critic:true,writing:true});
restoreWorkflowStages({workflowStages:{review:true,author:false,critic:true,writing:false},problemMode:'latex'});
assert.deepEqual(selectedWorkflowStages(), {review:true,author:false,critic:true,writing:false});
restoreWorkflowStages({problemMode:'latex'});
assert.deepEqual(selectedWorkflowStages(), {review:false,author:false,critic:false,writing:true});
restoreWorkflowStages({statementReviewOnly:true});
assert.deepEqual(selectedWorkflowStages(), {review:true,author:false,critic:false,writing:false});
restoreWorkflowStages({skipStatementReview:true});
assert.deepEqual(selectedWorkflowStages(), {review:false,author:true,critic:true,writing:true});
""")

    def test_turning_off_the_active_prompt_moves_to_an_enabled_stage(self):
        self.run_app(self.STAGES, self.SETUP, r"""
ui.promptDialog.open = true;
activePrompt='author';
setWorkflowStages({review:false,author:false,critic:true,writing:true});
assert.equal(activePrompt,'critic');
setWorkflowStages({review:false,author:false,critic:false,writing:true});
assert.equal(activePrompt,'final');
""")

    def test_changing_stages_during_create_keeps_duplicate_submissions_blocked(self):
        self.run_app(self.STAGES, self.SETUP, r"""
ui.check.dataset.creating = '1';
ui.check.textContent = 'Starting…';
setWorkflowStages({review:false,author:true,critic:false,writing:true});
assert(ui.check.disabled);
assert.equal(ui.check.textContent, 'Starting…');
delete ui.check.dataset.creating;
setWorkflowStages();
assert(!ui.check.disabled);
assert.equal(ui.check.textContent, 'Start workflow');
""")


if __name__ == '__main__':
    unittest.main()
