"""Private showcase history, separate from the original research database."""
import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def history_path(root):
    return Path(root) / 'data' / 'local_experiments.sqlite'


REPORT_FIELDS = ('source', 'decision', 'resources', 'comparison', 'comparisons_by_mode',
                 'answers', 'diagnostics', 'errors', 'actual_api_calls', 'baseline_available',
                 'greenai_available', 'effective_optimization', 'evidence', 'caution',
                 'mock_pricing_timestamp', 'quality_evaluation', 'gateway_pricing',
                 'request_fingerprint', 'experiment_id', 'planned_modes')


def experiment_key(report):
    return report.get('experiment_id') or hashlib.sha256(
        json.dumps(report, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def strategy_modes(report):
    return list(dict.fromkeys(report.get('planned_modes') or
                [*report.get('answers', {}), *report.get('errors', {})]))


def pending_count(entry):
    reviewed = {r['run_id'] for r in entry['reviews']}
    return sum(r['run_id'] not in reviewed for r in entry['report']['resources'])


class ShowcaseHistory:
    def __init__(self, root):
        self.path = history_path(root)

    def list(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT payload FROM experiments ORDER BY created_at DESC, id DESC').fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, report, request_input, reviews=()):
        """Idempotent insertion; repeated saves merge reviews, never replace results."""
        key = experiment_key(report)
        now = datetime.now(timezone.utc).isoformat()
        entry = {'id': key, 'created_at': now,
                 'request': {k: request_input.get(k, '') for k in ('prompt', 'context')},
                 'report': {k: copy.deepcopy(report[k]) for k in REPORT_FIELDS if k in report},
                 'reviews': []}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO experiments VALUES (?, ?, ?)',
                         (key, now, json.dumps(entry, ensure_ascii=False)))
            self._append_reviews(conn, key, reviews)
        return key

    def append_review(self, key, review):
        # Never recreate a missing experiment with an incomplete payload.
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._append_reviews(conn, key, [review])

    @staticmethod
    def _append_reviews(conn, key, reviews):
        row = conn.execute('SELECT payload FROM experiments WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('未找到已保存的实验。')
        entry = json.loads(row[0])
        for review in reviews:
            if review not in entry['reviews']:
                entry['reviews'].append(copy.deepcopy(review))
        conn.execute('UPDATE experiments SET payload=? WHERE id=?', (json.dumps(entry, ensure_ascii=False), key))
