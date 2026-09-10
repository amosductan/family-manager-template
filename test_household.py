"""Workflow regression checks; synthetic data, no network or production DB writes.

Runs against the example family (Sam and Jordan; Ava and Leo) whatever a user has put in
their own data/household.json, so the checks pass the same way on every machine.

    python test_household.py
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
import io

TEST_DIR = tempfile.TemporaryDirectory(prefix='family-manager-tests-')
os.environ['FM_DATA_DIR'] = TEST_DIR.name
os.environ['FM_INGEST_EVERY_HOURS'] = '0'
os.environ['AUTH_MODE'] = 'dev'

# The example family has to be in place BEFORE app/household import: their module-level
# names (app.KIDS, household.PARENTS) are read from family at import.
import family
family.use_example()

import app as server
import db
import household

PARENT, OTHER = family.PARENTS[0], family.PARENTS[1]    # Sam, Jordan
KID, SECOND = family.KIDS[0], family.KIDS[1]            # Ava, Leo


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect()
        for table in ('household_tasks', 'household_history', 'mail_actions', 'emails', 'events',
                      'checklist', 'gathering_items', 'gatherings', 'coverage_plan', 'sitters',
                      'payments', 'payment_plans', 'payment_observations', 'household_reviews', 'kid_routines'):
            self.con.execute('DELETE FROM ' + table)
        self.today = date.today()
        self.future = (self.today + timedelta(days=5)).isoformat()
        self.past = (self.today - timedelta(days=5)).isoformat()
        self.con.execute("INSERT INTO emails(id,msg_id,subject) VALUES (1,'message','School note')")
        self.con.execute("INSERT INTO mail_actions(id,msg_id,idx,text,due,state) VALUES (1,'message',0,'Return permission slip',?,'open')", (self.future,))
        self.con.execute("INSERT INTO checklist(id,title,kid,status) VALUES (1,'Bring a book',?,'open')", (KID,))
        self.con.execute("INSERT INTO gatherings(slug,name) VALUES ('party','Birthday')")
        self.con.execute("INSERT INTO gathering_items(id,gathering,title,status) VALUES (1,'party','Order cake','open')")
        self.con.execute("INSERT INTO payments(id,name,kid,amount,cadence,active) VALUES (1,'Camp',?,100,'weekly',1)", (SECOND,))
        self.con.execute("INSERT INTO events(kid,title,event_date,type,category,status) VALUES (?,'School closed',?,'closure','kids_school','active')", (KID, self.future))
        self.con.commit()
        self.client = server.app.test_client()

    def tearDown(self):
        self.con.close()

    def change(self, key, action, **extra):
        return self.client.post('/actions/change', json=dict(key=key, action=action, who=PARENT, **extra))

    def test_example_family_is_loaded(self):
        self.assertEqual(family.PARENTS, ['Sam', 'Jordan'])
        self.assertEqual(family.KIDS, ['Ava', 'Leo'])
        self.assertEqual(server.KIDS, family.KIDS)

    def test_all_sources_and_source_completion(self):
        keys = {t['key'] for t in household.collect(self.con)}
        self.assertTrue({'mail:1', 'checklist:1', 'gathering:1', 'coverage:' + self.future} <= keys)
        for key in ('mail:1', 'checklist:1', 'gathering:1'):
            self.assertEqual(self.change(key, 'done').status_code, 200)
        self.assertEqual(self.con.execute('SELECT state FROM mail_actions').fetchone()[0], 'done')
        self.assertEqual(self.con.execute('SELECT status FROM checklist').fetchone()[0], 'done')
        self.assertEqual(self.con.execute('SELECT status FROM gathering_items').fetchone()[0], 'done')
        self.assertEqual(self.change('mail:1', 'undo').status_code, 200)
        self.assertEqual(self.con.execute('SELECT state FROM mail_actions').fetchone()[0], 'open')

    def test_owner_snooze_and_reingest(self):
        self.assertEqual(self.change('gathering:1', 'owner', owner=OTHER).status_code, 200)
        self.assertEqual(self.con.execute('SELECT owner FROM gathering_items').fetchone()[0], OTHER)
        self.change('mail:1', 'claim')
        self.change('mail:1', 'snooze', until=self.future)
        self.assertTrue(next(t for t in household.collect(self.con) if t['key'] == 'mail:1')['snoozed'])
        self.change('mail:1', 'done')
        db.save_summary(self.con, 'message', {'action_items': []}, KID)
        self.con.commit()
        self.assertEqual(self.con.execute('SELECT state FROM mail_actions').fetchone()[0], 'done')
        self.assertEqual(self.change('mail:1', 'snooze', until=self.past).status_code, 400)

    def test_owner_must_be_a_parent(self):
        r = self.change('gathering:1', 'owner', owner='Somebody Else')
        self.assertEqual(r.status_code, 400)
        self.assertIn(PARENT, r.json['error'])

    def test_blocked_and_actor(self):
        self.con.execute("UPDATE checklist SET status='blocked',blocked_on='Wait for teacher'")
        self.con.commit()
        self.assertEqual(self.change('checklist:1', 'done').status_code, 400)
        self.assertEqual(self.client.post('/actions/change', json={'key': 'mail:1', 'action': 'done'}).status_code, 400)
        # A name that isn't one of this household's parents is refused, not recorded.
        self.assertEqual(self.client.post('/actions/change', json={'key': 'mail:1', 'action': 'done', 'who': 'Stranger'}).status_code, 400)

    def test_mail_reordering_and_disappearing_claimed_ask(self):
        summary = {'action_items': [{'text': 'Return permission slip', 'kid': KID}, {'text': 'Buy supplies', 'kid': KID}]}
        db.save_summary(self.con, 'message', summary, KID)
        self.con.commit()
        original = self.con.execute("SELECT id FROM mail_actions WHERE text='Return permission slip'").fetchone()[0]
        self.change(f'mail:{original}', 'claim')
        summary['action_items'].reverse()
        db.save_summary(self.con, 'message', summary, KID)
        self.con.commit()
        task = next(t for t in household.collect(self.con) if t['key'] == f'mail:{original}')
        self.assertEqual(task['title'], 'Return permission slip')
        self.assertEqual(task['owner'], PARENT)
        db.save_summary(self.con, 'message', {'action_items': []}, KID)
        self.con.commit()
        self.assertTrue(any(t['key'] == f'mail:{original}' for t in household.collect(self.con)))

    def test_waiting_expiring_reminder_and_undo(self):
        self.change('checklist:1', 'waiting')
        self.assertEqual(next(t for t in household.collect(self.con) if t['key'] == 'checklist:1')['stage'], 'waiting')
        self.change('checklist:1', 'snooze', until=self.future)
        later = household.collect(self.con, date.fromisoformat(self.future))
        self.assertFalse(next(t for t in later if t['key'] == 'checklist:1')['snoozed'])
        self.change('checklist:1', 'done')
        self.assertEqual(self.change('checklist:1', 'undo').status_code, 200)
        self.assertEqual(self.con.execute('SELECT status FROM checklist').fetchone()[0], 'open')

    def test_undo_refuses_later_source_edit(self):
        self.change('gathering:1', 'done')
        self.con.execute("UPDATE gathering_items SET owner=?", (OTHER,))
        self.con.commit()
        self.assertEqual(self.change('gathering:1', 'undo').status_code, 400)
        self.assertEqual(self.con.execute('SELECT owner FROM gathering_items').fetchone()[0], OTHER)

    def test_backlog_review_preserves_obligation(self):
        self.con.execute('UPDATE mail_actions SET due=?', (self.past,))
        self.con.commit()
        self.assertTrue(next(t for t in household.collect(self.con) if t['key'] == 'mail:1')['review'])
        self.change('mail:1', 'review')
        t = next(t for t in household.collect(self.con) if t['key'] == 'mail:1')
        self.assertFalse(t['review'])
        self.assertEqual(t['state'], 'open')

    def test_coverage_confirmation_and_calendar_gate(self):
        endpoint = '/days-off/plan'
        self.client.post(endpoint, json={'day': self.future, 'coverage': PARENT, 'who': PARENT})
        blocks, skipped = server._blocks_for(self.con, [self.future], 'full')
        self.assertEqual(blocks, [])
        self.assertTrue(skipped)
        self.assertEqual(self.change('coverage:' + self.future, 'done').status_code, 400)
        response = self.client.post(endpoint, json={'day': self.future, 'confirmation': 'confirmed', 'who': PARENT})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(server._blocks_for(self.con, [self.future], 'full')[0]), 1)
        response = self.client.post(endpoint, json={'day': self.future, 'coverage': OTHER, 'who': PARENT})
        self.assertEqual(response.json['confirmation'], 'proposed')
        self.client.post(endpoint, json={'day': self.future, 'coverage': 'Babysitter', 'who': PARENT})
        self.assertEqual(self.client.post(endpoint, json={'day': self.future, 'confirmation': 'confirmed', 'who': PARENT}).status_code, 400)

    def test_closure_suppresses_routines(self):
        self.con.execute("INSERT INTO kid_routines(kid,weekday,label) VALUES (?,?,'Library')", (KID, date.fromisoformat(self.future).weekday()))
        self.con.commit()
        self.assertEqual(server.school_today_lines(self.con, date.fromisoformat(self.future)), [])
        self.con.execute("UPDATE events SET status='cancelled'")
        self.con.commit()
        self.assertEqual(server.school_today_lines(self.con, date.fromisoformat(self.future))[0]['today'][0]['label'], 'Library')

    def test_transition_comparison_and_duplicate_import(self):
        response = self.client.post('/payment-plans', data=dict(action='plan', who=PARENT, payment_id=1, change_date=self.future, expected_amount='125', ends=self.future))
        self.assertEqual(response.status_code, 302)
        data = dict(action='observe', who=PARENT, payment_id=1, charged_on=self.future, amount='125', evidence='statement row 1')
        self.client.post('/payment-plans', data=data)
        self.client.post('/payment-plans', data=data)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM payment_observations').fetchone()[0], 1)
        self.assertEqual(household.plans(self.con)[0]['observations'][0]['finding'], 'Matches the expected amount')
        data.update(charged_on=(date.fromisoformat(self.future) + timedelta(days=1)).isoformat(), evidence='row 2')
        self.client.post('/payment-plans', data=data)
        self.assertEqual(household.plans(self.con)[0]['observations'][0]['finding'], 'Outside the expected activity dates')
        data.update(amount='nan')
        self.assertEqual(self.client.post('/payment-plans', data=data).status_code, 400)

    def test_csv_atomic_validation_and_weekly_plan(self):
        self.assertEqual(self.client.post('/payment-plans', data={'action': 'upload', 'payment_id': '1', 'who': PARENT, 'statement': (io.BytesIO(b'charged_on,amount,evidence\n2026-09-01,100,row1\nbad,100,row2'), 'charges.csv')}).status_code, 400)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM payment_observations').fetchone()[0], 0)
        plan = f'{PARENT} handles pickup'
        self.assertEqual(self.client.post('/weekly-review', json={'who': OTHER, 'plan': plan, 'burden': '2'}).status_code, 200)
        self.assertEqual(self.con.execute('SELECT plan FROM household_reviews').fetchone()[0], plan)

    def test_pages_render_with_real_rows(self):
        for path in ('/', '/actions', '/actions?view=review', '/actions?view=handled', '/weekly-review',
                     '/payment-plans', '/payments', '/days-off', '/kid/' + KID, '/kid/' + SECOND,
                     '/checklist', '/scan', '/privacy'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, (path, response.data[:400]))

    def test_pages_carry_the_household_not_a_fixed_family(self):
        body = self.client.get('/').get_data(as_text=True)
        for name in family.PARENTS + family.KIDS:
            self.assertIn(name, body)
        self.assertIn(family.FAMILY_NAME, body)
        self.assertIn('Showing the example family', body)
        self.assertIn('window.FM_PARENTS = ["Sam", "Jordan"]', body)
        # Every kid gets a nav link, and a name that isn't a kid is sent home.
        self.assertIn('href="/kid/Leo"', body)
        self.assertEqual(self.client.get('/kid/Nobody').status_code, 302)

    def test_kid_chip_classes_are_positional(self):
        self.assertEqual(family.kid_class(KID), 'kid-0')
        self.assertEqual(family.kid_class(SECOND), 'kid-1')
        self.assertEqual(family.kid_class('Both'), 'kid-both')
        body = self.client.get('/checklist').get_data(as_text=True)
        self.assertIn('chip kid-0', body)


if __name__ == '__main__':
    unittest.main()
