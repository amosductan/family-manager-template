"""Shared household workflow. Source rows own completion; this table owns coordination.

No network calls on reads. Changes are explicit, attributed, and audited. Undo refuses
to overwrite a later change.
"""
import csv
import hashlib
import io
import json
import math
from datetime import date, timedelta
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request, redirect
import db
import family

bp = Blueprint('household', __name__)
H = {}
# Kept as a module name for importers; the check itself reads family.PARENTS at call time
# so a household installed after import (family.reload) is the one that counts.
PARENTS = family.PARENTS


def _parents():
    return list(family.PARENTS)


def iso(value, optional=True):
    if not value and optional:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Use a valid date.')


def amount(value, optional=False):
    if value in ('', None) and optional:
        return None
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError('Enter an amount in dollars.')
    if not math.isfinite(n) or n < 0 or n > 1000000:
        raise ValueError('Enter a non-negative amount below $1,000,000.')
    return round(n, 2)


def actor(data):
    who = data.get('who')
    if who not in _parents():
        raise ValueError('Choose your name in the parent picker first.')
    return who


def collect(con, today=None):
    today = today or date.today()
    tasks = []

    def add(key, title, source, href, state='open', due=None, kid=None,
            created=None, owner=None, blocked=None):
        tasks.append(dict(key=key, title=title, source=source, href=href, state=state,
                          due=due, kid=kid, created=created, owner=owner, blocked=blocked))

    for r in con.execute('SELECT a.*, e.id eid FROM mail_actions a JOIN emails e ON e.msg_id=a.msg_id'):
        add(f'mail:{r["id"]}', r['text'], 'School & notes', f'/mail/{r["eid"]}',
            r['state'], r['due'], r['kid'], r['created_at'])
    for r in con.execute('SELECT * FROM checklist'):
        add(f'checklist:{r["id"]}', r['title'], 'Checklist', '/checklist',
            {'na': 'dismissed', 'blocked': 'open'}.get(r['status'], r['status']),
            r['due_date'], r['kid'], blocked=r['blocked_on'] if r['status'] == 'blocked' else None)
    for r in con.execute('SELECT i.*, g.name gathering_name FROM gathering_items i JOIN gatherings g ON g.slug=i.gathering'):
        add(f'gathering:{r["id"]}', r['title'], r['gathering_name'],
            '/gathering/' + quote(r['gathering']),
            'dismissed' if r['status'] == 'skipped' else ('done' if r['status'] == 'done' else 'open'),
            r['due'], owner=r['owner'])
    for r in H['_board'](con, today=today.isoformat()):
        # A work holiday alone is not a confirmed childcare arrangement.
        if r['day'] > (today + timedelta(days=60)).isoformat():
            continue
        add('coverage:' + r['day'], f'{r["who"]}: {"early pickup" if r["kind"] == "early" else "school closed"}',
            'Coverage', '/days-off', 'done' if r.get('confirmation') == 'confirmed' else 'open',
            r['day'], r['who'])
    meta = {r['key']: dict(r) for r in con.execute('SELECT * FROM household_tasks')}
    for t in tasks:
        m = meta.get(t['key'], {})
        # Gathering owner remains the source of truth, including unassignment there.
        if not t['key'].startswith('gathering:'):
            t['owner'] = m.get('owner')
        t['stage'] = m.get('stage') or 'open'
        t['snooze_until'] = m.get('snooze_until')
        t['snoozed'] = bool(t['snooze_until'] and t['snooze_until'] > today.isoformat())
        t['overdue'] = bool(t['due'] and t['due'] < today.isoformat())
        old_undated = not t['due'] and bool(t['created'] and t['created'][:10] < (today - timedelta(days=14)).isoformat())
        recently_reviewed = bool(m.get('reviewed_at') and m['reviewed_at'][:10] >= (today - timedelta(days=7)).isoformat())
        t['review'] = (t['overdue'] or old_undated) and not recently_reviewed and t['state'] == 'open'
    return sorted(tasks, key=lambda t: (t['snoozed'], t['stage'] == 'waiting', not t['overdue'], t['due'] or '9999', t['title'] or ''))


def source_snapshot(con, key):
    kind, ident = key.split(':', 1)
    mapping = {
        'mail': ('mail_actions', ('state', 'done_by', 'done_at')),
        'checklist': ('checklist', ('status', 'done_by', 'done_at')),
        'gathering': ('gathering_items', ('status', 'owner', 'updated_by', 'updated_at')),
    }
    if kind == 'coverage':
        return {}
    table, cols = mapping[kind]
    row = con.execute(f'SELECT {",".join(cols)} FROM {table} WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('This item no longer exists.')
    return dict(row)


def source_write(con, key, values):
    kind, ident = key.split(':', 1)
    if kind == 'coverage':
        return
    table = {'mail': 'mail_actions', 'checklist': 'checklist', 'gathering': 'gathering_items'}[kind]
    con.execute(f'UPDATE {table} SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                (*values.values(), ident))


def snapshot(con, key):
    m = con.execute('SELECT * FROM household_tasks WHERE key=?', (key,)).fetchone()
    return dict(source=source_snapshot(con, key), meta=dict(m) if m else None)


def change(con, key, action, who, data):
    tasks = {t['key']: t for t in collect(con)}
    if key not in tasks:
        raise ValueError('This task is no longer in the source. Refresh the page.')
    t = tasks[key]
    before = snapshot(con, key)
    if action == 'undo':
        log = con.execute('SELECT * FROM household_history WHERE task_key=? AND undone=0 ORDER BY id DESC LIMIT 1', (key,)).fetchone()
        if not log:
            raise ValueError('There is no change to undo.')
        if before != json.loads(log['after_json']):
            raise ValueError('This item changed elsewhere. Refresh and review it before changing it again.')
        old = json.loads(log['before_json'])
        source_write(con, key, old['source'])
        con.execute('DELETE FROM household_tasks WHERE key=?', (key,))
        if old['meta']:
            m = old['meta']
            con.execute('INSERT INTO household_tasks (' + ','.join(m) + ') VALUES (' + ','.join('?' for _ in m) + ')', tuple(m.values()))
        con.execute('UPDATE household_history SET undone=1 WHERE id=?', (log['id'],))
        con.execute('INSERT INTO household_history(task_key,action,before_json,after_json,who,at,undone) VALUES (?,?,?,?,?,?,1)',
                    (key, 'undo', json.dumps(before), json.dumps(old), who, db.now()))
        return
    if action not in ('claim', 'owner', 'waiting', 'ready', 'snooze', 'review', 'done', 'dismiss', 'reopen'):
        raise ValueError('Unknown task action.')
    if key.startswith('coverage:') and action in ('done', 'dismiss', 'reopen'):
        raise ValueError('Confirm the actual arrangement on Days off. A task tick cannot confirm coverage.')
    if t['blocked'] and action == 'done':
        raise ValueError('Blocked: ' + t['blocked'])
    con.execute('INSERT OR IGNORE INTO household_tasks(key) VALUES (?)', (key,))
    m = dict(con.execute('SELECT * FROM household_tasks WHERE key=?', (key,)).fetchone())
    src = before['source'].copy()
    if action in ('claim', 'owner'):
        owner = who if action == 'claim' else data.get('owner') or None
        if owner is not None and owner not in _parents():
            raise ValueError('Choose ' + ', '.join(_parents()) + ', or Unassigned.')
        m['owner'] = owner
        if key.startswith('gathering:'):
            src['owner'] = owner
            src.update(updated_by=who, updated_at=db.now())
    elif action in ('waiting', 'ready'):
        m['stage'] = 'waiting' if action == 'waiting' else 'open'
        m['snooze_until'] = None
    elif action == 'snooze':
        until = iso(data.get('until'), False)
        if until <= date.today().isoformat():
            raise ValueError('Choose a future reminder date.')
        m['snooze_until'] = until
    elif action == 'review':
        m.update(reviewed_at=db.now(), snooze_until=None)
    else:
        state = {'done': 'done', 'dismiss': 'dismissed', 'reopen': 'open'}[action]
        kind = key.split(':')[0]
        if kind == 'mail':
            src.update(state=state, done_by=who if state != 'open' else None, done_at=db.now() if state != 'open' else None)
        elif kind == 'checklist':
            src.update(status='na' if state == 'dismissed' else state, done_by=who if state != 'open' else None, done_at=db.now() if state != 'open' else None)
        else:
            src.update(status='skipped' if state == 'dismissed' else state, updated_by=who, updated_at=db.now())
        m.update(stage='open', snooze_until=None)
    m.update(updated_by=who, updated_at=db.now())
    con.execute('UPDATE household_tasks SET ' + ','.join(k + '=?' for k in m if k != 'key') + ' WHERE key=?',
                (*[v for k, v in m.items() if k != 'key'], key))
    if src != before['source']:
        source_write(con, key, src)
    after = snapshot(con, key)
    con.execute('INSERT INTO household_history(task_key,action,before_json,after_json,who,at) VALUES (?,?,?,?,?,?)',
                (key, action, json.dumps(before), json.dumps(after), who, db.now()))


def plans(con, today=None):
    today = today or date.today()
    result = []
    for r in con.execute('SELECT p.*, t.starts,t.ends,t.change_date,t.expected_amount,t.change_note,t.reviewed_at FROM payments p LEFT JOIN payment_plans t ON t.payment_id=p.id WHERE p.active=1 ORDER BY p.name'):
        p = dict(r)
        p['upcoming'] = any(p[k] and today.isoformat() <= p[k] <= (today + timedelta(days=30)).isoformat() for k in ('starts', 'ends', 'change_date'))
        p['observations'] = []
        for rr in con.execute('SELECT * FROM payment_observations WHERE payment_id=? ORDER BY charged_on DESC,id DESC', (p['id'],)):
            o = dict(rr)
            expected = p['expected_amount'] if p['change_date'] and o['charged_on'] >= p['change_date'] else p['amount']
            o['expected'] = expected
            if (p['ends'] and o['charged_on'] > p['ends']) or (p['starts'] and o['charged_on'] < p['starts']):
                o['finding'] = 'Outside the expected activity dates'
            elif expected is None:
                o['finding'] = 'Expected amount unknown'
            elif abs(o['amount'] - expected) > .009:
                o['finding'] = 'Amount differs — review the statement'
            else:
                o['finding'] = 'Matches the expected amount'
            p['observations'].append(o)
        result.append(p)
    return result


def install(app, helpers):
    H.update(helpers)
    app.register_blueprint(bp)


@bp.get('/actions')
def actions():
    con = db.connect()
    try:
        tasks = collect(con)
        view = request.args.get('view', 'open')
        selected = request.args.get('owner', '')
        counts = dict(open=sum(t['state'] == 'open' and not t['snoozed'] and not t['review'] for t in tasks),
                      review=sum(t['review'] for t in tasks),
                      later=sum(t['state'] == 'open' and t['snoozed'] for t in tasks),
                      handled=sum(t['state'] != 'open' for t in tasks))
        rows = [t for t in tasks if (t['review'] if view == 'review' else
                t['state'] == 'open' and t['snoozed'] if view == 'later' else
                t['state'] != 'open' if view == 'handled' else t['state'] == 'open' and not t['snoozed'] and not t['review'])]
        if selected:
            rows = [t for t in rows if (t['owner'] or 'Unassigned') == selected]
        page = max(1, request.args.get('page', 1, type=int) or 1)
        pages = max(1, (len(rows) + 24) // 25)
        page = min(page, pages)
        total = len(rows)
        return render_template('actions.html', tasks=rows[(page-1)*25:page*25], counts=counts,
                               view=view, selected=selected, page=page, pages=pages, total=total)
    finally:
        con.close()


@bp.post('/actions/change')
def action_change():
    data = request.get_json(silent=True) or {}
    con = db.connect()
    try:
        who = actor(data)
        # BEGIN IMMEDIATE takes the write lock up front, so two parents tapping the same
        # task at once serialize here instead of both reading the same "before".
        con.execute('BEGIN IMMEDIATE')
        change(con, data.get('key', ''), data.get('action'), who, data)
        con.commit()
        return jsonify(ok=True)
    except (ValueError, KeyError) as exc:
        con.rollback()
        return jsonify(error=str(exc)), 400
    finally:
        con.close()


@bp.route('/weekly-review', methods=['GET', 'POST'])
def weekly_review():
    today = date.today()
    week = (today - timedelta(days=today.weekday())).isoformat()
    con = db.connect()
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            who = actor(data)
            burden = data.get('burden')
            if burden not in (None, '', '1', '2', '3', '4', '5'):
                raise ValueError('Choose a tracking-burden score from 1 to 5.')
            con.execute('INSERT INTO household_reviews(week,plan,burden,updated_by,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(week) DO UPDATE SET plan=excluded.plan,burden=excluded.burden,updated_by=excluded.updated_by,updated_at=excluded.updated_at',
                        (week, str(data.get('plan') or '')[:5000], int(burden) if burden else None, who, db.now()))
            con.commit()
            return jsonify(ok=True)
        tasks = [t for t in collect(con) if t['state'] == 'open']
        horizon = (today + timedelta(days=14)).isoformat()
        attention = [t for t in tasks if t['due'] and today.isoformat() <= t['due'] <= horizon]
        return render_template('weekly_review.html', week=week, attention=attention,
                               unassigned=sum(not t['owner'] for t in tasks),
                               transitions=[p for p in plans(con) if p['upcoming']],
                               reviews=con.execute('SELECT * FROM household_reviews ORDER BY week DESC LIMIT 8').fetchall(),
                               review=con.execute('SELECT * FROM household_reviews WHERE week=?', (week,)).fetchone())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    finally:
        con.close()


@bp.route('/payment-plans', methods=['GET', 'POST'])
def payment_plans():
    con = db.connect()
    try:
        if request.method == 'GET':
            return render_template('payment_plans.html', plans=plans(con))
        data = request.form
        who = actor(data)
        pid = int(data.get('payment_id', '0'))
        if not con.execute('SELECT id FROM payments WHERE id=?', (pid,)).fetchone():
            raise ValueError('Choose an existing payment.')
        action = data.get('action')
        if action == 'plan':
            starts, ends, change_date = (iso(data.get(k)) for k in ('starts', 'ends', 'change_date'))
            if starts and ends and ends < starts:
                raise ValueError('The end date must follow the start date.')
            expected = amount(data.get('expected_amount'), optional=True)
            if expected is not None and not change_date:
                raise ValueError('Set the date the new amount takes effect.')
            con.execute('INSERT INTO payment_plans(payment_id,starts,ends,change_date,expected_amount,change_note,reviewed_at,updated_by) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(payment_id) DO UPDATE SET starts=excluded.starts,ends=excluded.ends,change_date=excluded.change_date,expected_amount=excluded.expected_amount,change_note=excluded.change_note,reviewed_at=excluded.reviewed_at,updated_by=excluded.updated_by',
                        (pid, starts, ends, change_date, expected, data.get('change_note', '')[:1000], db.now(), who))
        elif action in ('observe', 'upload'):
            if action == 'upload':
                upload = request.files.get('statement')
                if not upload:
                    raise ValueError('Choose a CSV with charged_on, amount, evidence columns.')
                raw = upload.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('Keep this selected-charge CSV under 1 MB.')
                reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
                if not {'charged_on', 'amount', 'evidence'} <= set(reader.fieldnames or []):
                    raise ValueError('CSV columns must include charged_on, amount, evidence.')
                rows = list(reader)
                if len(rows) > 1000:
                    raise ValueError('Import at most 1,000 selected charges at a time.')
            else:
                rows = [data]
            prepared = []
            occurrences = {}
            for r in rows:
                charged = iso(r.get('charged_on'), False)
                paid = amount(r.get('amount'))
                evidence = str(r.get('evidence') or '').strip()[:200]
                if not evidence:
                    raise ValueError('Give each charge a statement or transaction reference.')
                base = json.dumps([pid, charged, paid, evidence])
                ordinal = occurrences.get(base, 0)
                occurrences[base] = ordinal + 1
                fingerprint = hashlib.sha256((base + ':' + str(ordinal)).encode()).hexdigest()
                prepared.append((pid, charged, paid, evidence, fingerprint, who, db.now()))
            con.executemany('INSERT OR IGNORE INTO payment_observations(payment_id,charged_on,amount,evidence,fingerprint,recorded_by,recorded_at) VALUES (?,?,?,?,?,?,?)', prepared)
        elif action == 'remove_observation':
            con.execute('DELETE FROM payment_observations WHERE id=? AND payment_id=?', (data.get('observation_id'), pid))
        else:
            raise ValueError('Unknown payment action.')
        con.commit()
        return redirect('/payment-plans#payment-' + str(pid))
    except (ValueError, UnicodeError) as exc:
        con.rollback()
        return render_template('payment_plans.html', plans=plans(con), error=str(exc)), 400
    finally:
        con.close()
