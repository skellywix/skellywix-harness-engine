import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from skellywix import engine as e
from skellywix.desk import render


class RoutingContract(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / 'desk'
        e.initialize(self.home)

    def record(self, cohort, target, *tags):
        return e.record(self.home, cohort, target, tags)

    def route(self, *tags):
        guide = e.resolve(self.home, tags)['guide']
        return guide['id'] if guide else None

    def evidence(self):
        self.record('train', 'test-change', 'flaky', 'suite')
        self.record('check', 'test-change', 'flaky', 'intermittent')
        self.record('check', 'review-release', 'publish', 'bundle')

    def test_revision_changes_real_consumer_and_rolls_back(self):
        self.evidence()
        self.assertIsNone(self.route('flaky'))
        self.assertEqual(len(e.status(self.home)['queue']), 1)
        decision = e.run(self.home)
        self.assertEqual((decision['decision'], decision['before'], decision['after'], decision['checks']), ('promoted', 1, 2, 2))
        self.assertEqual(self.route('flaky'), 'test-change')
        self.assertEqual(e.status(self.home)['queue'], [])
        e.rollback(self.home)
        self.assertIsNone(self.route('flaky'))
        self.assertEqual(e.run(self.home)['decision'], 'unchanged')
        self.assertIsNone(self.route('flaky'))

    def test_net_gain_cannot_hide_losing_a_formerly_correct_case(self):
        self.evidence()
        self.record('check', 'test-change', 'flaky', 'runner')
        self.record('check', 'none', 'flaky', 'unrelated')
        decision = e.run(self.home)
        self.assertGreater(decision['after'], decision['before'])
        self.assertEqual((decision['decision'], decision['regressions']), ('held', 1))
        self.assertIsNone(self.route('flaky'))

    def test_no_check_means_no_promotion(self):
        self.record('train', 'inspect-logs', 'timing')
        self.assertEqual(e.run(self.home)['decision'], 'held')
        self.assertIsNone(self.route('timing'))

    def test_deduplicates_and_rejects_split_or_label_leakage(self):
        self.record('train', 'test-change', 'flaky', 'suite')
        self.record('train', 'test-change', 'suite', 'flaky')
        for cohort, target in [('check', 'test-change'), ('train', 'inspect-logs')]:
            with self.assertRaises(ValueError):
                self.record(cohort, target, 'flaky', 'suite')
        self.assertEqual(e.status(self.home)['counts'], {'train': 1, 'check': 0})

    def test_conflicting_training_and_ties_abstain(self):
        self.record('train', 'test-change', 'sporadic', 'suite')
        self.record('train', 'inspect-logs', 'sporadic', 'metric')
        self.record('check', 'test-change', 'sporadic', 'worker')
        e.run(self.home)
        self.assertIsNone(self.route('sporadic'))
        self.assertIsNone(self.route('publish', 'regression'))

    def test_two_workers_cannot_promote_twice(self):
        self.evidence()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(e.run, [self.home, self.home]))
        self.assertCountEqual([r['decision'] for r in results], ['promoted', 'unchanged'])
        self.assertEqual(self.route('flaky'), 'test-change')
        self.assertEqual(sum(r['decision'] == 'promoted' for r in e.status(self.home)['history']), 1)

    def test_reopen_cannot_seed_an_existing_workspace(self):
        self.evidence()
        e.run(self.home)
        e.initialize(self.home)
        with self.assertRaises(ValueError):
            e.initialize(self.home, demo=True)
        self.assertEqual(self.route('flaky'), 'test-change')
        self.assertEqual(e.status(self.home)['counts'], {'train': 1, 'check': 2})

    def test_rejects_unrelated_directory_and_raw_input(self):
        unrelated = self.home.parent / 'existing'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_text('untouched', encoding='utf-8')
        with self.assertRaises(ValueError):
            e.initialize(unrelated)
        self.assertEqual([p.name for p in unrelated.iterdir()], ['keep.txt'])
        for tags in [[], ['../sample'], ['note from a chat'], ['a' * 33], ['tag'] * 9]:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                e.record(self.home, 'train', 'test-change', tags)
        with self.assertRaises(ValueError):
            self.record('train', 'not-a-guide', 'legit')
        self.assertEqual(e.status(self.home)['counts'], {'train': 0, 'check': 0})

    def test_owner_guide_is_usable_but_never_markup(self):
        g = {'id': 'verify-format', 'title': 'Check <format>', 'tags': ['format'], 'advice': 'Compare <script>alert(1)</script> as text.'}
        e.add_guide(self.home, g)
        self.assertEqual(e.resolve(self.home, ['format'])['guide'], g)
        html = render(e.status(self.home))
        self.assertIn('Check &lt;format&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        with self.assertRaises(ValueError):
            e.add_guide(self.home, {**g, 'advice': 'replace'})

    def test_checked_abstention_is_not_an_unknown_guide(self):
        self.record('check', 'none', 'unfamiliar')
        self.assertEqual(e.status(self.home)['queue'], [])
        with self.assertRaises(ValueError):
            self.record('train', 'none', 'unowned')


if __name__ == '__main__':
    unittest.main()
