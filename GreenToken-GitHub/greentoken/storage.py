import json, sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from datetime import datetime, timezone
from .pricing import COST_FIELDS
from .diagnostics import diagnose_record
from .human_evaluation import parse_assessment, encode_assessment, legacy_status, SCORED_STATUSES

@contextmanager
def database_connection(path, read_only=False):
    """Retain commit/rollback semantics and always release the file handle."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) if read_only else sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def execute(db, sql, parameters=(), fetch=None):
    """Materialize results before explicitly closing each cursor."""
    with closing(db.cursor()) as cursor:
        cursor.execute(sql, parameters)
        if fetch == "one":
            return cursor.fetchone()
        if fetch == "all":
            return cursor.fetchall()


class Store:
    def __init__(self, path, read_only=False):
        self.path = path
        self.read_only = read_only
        if read_only:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Back up existing database once before additive v2 migration.
        if Path(path).exists():
            with self.connect() as db:
                old_version = execute(db, "PRAGMA user_version", fetch="one")[0]
                if old_version < 2:
                    backup = str(path) + ".pre-v02.bak"
                    if not Path(backup).exists():
                        with database_connection(backup) as target:
                            db.backup(target)
        with self.connect() as db:
            execute(db, "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            execute(db, "CREATE TABLE IF NOT EXISTS failures (data TEXT NOT NULL)")
            execute(db, "CREATE TABLE IF NOT EXISTS ratings (run_id TEXT, score REAL, note TEXT, created_at TEXT)")
            version = execute(db, "PRAGMA user_version", fetch="one")[0]
            if version < 2:
                execute(db, "PRAGMA user_version=2")
    def connect(self):
        return database_connection(self.path, self.read_only)
    def save(self, run):
        with self.connect() as db:
            execute(db, "INSERT INTO runs VALUES (?, ?)", (run["run_id"], json.dumps(run, ensure_ascii=False)))
    def runs(self):
        with self.connect() as db:
            rows = execute(db, "SELECT data FROM runs ORDER BY rowid DESC", fetch="all")
            result = [json.loads(row[0]) for row in rows]
            for index, old_run in enumerate(result):
                run = normalize(old_run)
                rating = execute(db, "SELECT score, note FROM ratings WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run["run_id"],), fetch="one")
                result[index] = run
                run["quality"] = {"method": "manual", "score": rating[0], "note": rating[1]} if rating else run["quality"]
                run["quality_score"] = run["quality"].get("score")
                history = self._completion_history(db, run['run_id'])
                run['completion_evaluation_history'] = history
                if history:
                    latest = history[-1]
                    run['task_completion_status'] = latest['status']
                    run['completion_source'] = 'manual'
                    run['needs_manual_evaluation'] = latest['status'] == 'pending'
                    run['human_evaluation'] = parse_assessment(latest['note'])
            return result
    def rate(self, run_id, score, note=""):
        if not 1 <= score <= 5:
            raise ValueError("Score must be 1–5")
        with self.connect() as db:
            if not execute(db, "SELECT 1 FROM runs WHERE id=?", (run_id,), fetch="one"):
                raise ValueError("Unknown run_id")
            execute(db, "INSERT INTO ratings VALUES (?, ?, ?, ?)", (run_id, score, note, datetime.now(timezone.utc).isoformat()))

    def save_failure(self, failure):
        with self.connect() as db:
            execute(db, "INSERT INTO failures VALUES (?)", (json.dumps(failure, ensure_ascii=False),))

    def failures(self):
        with self.connect() as db:
            return [{**f, **diagnose_record(f, failure=True)} for row in
                    execute(db, "SELECT data FROM failures ORDER BY rowid DESC", fetch="all")
                    for f in [json.loads(row[0])]]

    def _completion_history(self, db, run_id):
        if not execute(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='completion_evaluations'", fetch='one'):
            return []
        result = [dict(evaluation_id=row[0], run_id=row[1], status=row[2], note=row[3], created_at=row[4])
                  for row in execute(db, 'SELECT rowid,run_id,status,note,created_at FROM completion_evaluations WHERE run_id=? ORDER BY rowid', (run_id,), fetch='all')]
        for item in result:
            parsed = parse_assessment(item['note'])
            if parsed is not None:
                item['human_evaluation'] = parsed
        return result

    def completion_history(self, run_id):
        with self.connect() as db:
            return self._completion_history(db, run_id)

    def evaluate_completion(self, run_id, status, note=''):
        if status not in ('pending', 'incomplete', 'completed'):
            raise ValueError('Unknown completion status')
        with self.connect() as db:
            row = execute(db, 'SELECT data FROM runs WHERE id=?', (run_id,), fetch='one')
            if row is None:
                raise ValueError('Unknown run_id')
            diagnosis = diagnose_record(json.loads(row[0]))
            if diagnosis['answer_present'] is not True and status == 'completed':
                raise ValueError('没有有效可见答案，不能确认任务完成')
            if diagnosis['answer_present'] is False and status == 'pending':
                raise ValueError('已确认空答案，任务状态必须为 incomplete')
            # Additive, lazy migration: the original run and pricing JSON never change.
            execute(db, "CREATE TABLE IF NOT EXISTS completion_evaluations (run_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','incomplete','completed')), note TEXT NOT NULL, created_at TEXT NOT NULL)")
            execute(db, 'INSERT INTO completion_evaluations VALUES (?,?,?,?)',
                    (run_id, status, note, datetime.now(timezone.utc).isoformat()))

    def evaluate_human(self, run_id, evaluation, notes_only=False):
        """Atomic append to existing tables. Old JSON, ratings and notes never change."""
        encoded = encode_assessment(evaluation)
        evaluation = parse_assessment(encoded)
        with self.connect() as db:
            raw = execute(db, 'SELECT data FROM runs WHERE id=?', (run_id,), fetch='one')
            if raw is None:
                raise ValueError('Unknown run_id')
            run = normalize(json.loads(raw[0]))
            history = self._completion_history(db, run_id)
            current = history[-1]['status'] if history else run['task_completion_status']
            if notes_only:
                previous = parse_assessment(history[-1]['note']) if history else None
                if previous is None:
                    raise ValueError('No structured evaluation to update')
                previous['notes'] = evaluation['notes']
                evaluation = previous
                encoded = encode_assessment(previous)
                status = current
            else:
                if evaluation['completion_status'] in SCORED_STATUSES and not (run.get('answer') or '').strip():
                    raise ValueError('没有有效可见答案，不能判为 Completed 或 Partially Completed')
                status = legacy_status(evaluation['completion_status'],current)
            execute(db, "CREATE TABLE IF NOT EXISTS completion_evaluations (run_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','incomplete','completed')), note TEXT NOT NULL, created_at TEXT NOT NULL)")
            now = datetime.now(timezone.utc).isoformat()
            execute(db,'INSERT INTO completion_evaluations VALUES (?,?,?,?)',(run_id,status,encoded,now))
            if not notes_only and evaluation['completion_status'] in SCORED_STATUSES:
                execute(db,'INSERT INTO ratings VALUES (?,?,?,?)',
                        (run_id,evaluation['quality_score'],evaluation['notes'],now))

    def update_human_analysis(self, run_id, comparison=None, effect=None):
        """Append pair judgments only; preserve completion, score, issues and notes."""
        with self.connect() as db:
            history=self._completion_history(db,run_id)
            previous=parse_assessment(history[-1]['note']) if history else None
            if previous is None:
                raise ValueError('请先明确保存结构化完成评价，不自动补判旧数据')
            encoded=encode_assessment({**previous,'baseline_comparison':comparison,'optimization_effect':effect})
            if encoded==encode_assessment(previous):
                return
            execute(db,'INSERT INTO completion_evaluations VALUES (?,?,?,?)',
                    (run_id,history[-1]['status'],encoded,datetime.now(timezone.utc).isoformat()))


def normalize(run):
    """Non-destructive read migration. Raw JSON and legacy mode remain intact."""
    r = dict(run)
    r.setdefault("difficulty", r.get("classification", {}).get("level"))
    r.setdefault("context", r.get("original_context", ""))
    r.setdefault("actual_messages", r.get("sent_messages", []))
    r.setdefault("system_instruction", next((m["content"] for m in r["actual_messages"] if m["role"] == "system"), None))
    r.setdefault("possible_truncation", r.get("finish_reason") == "length")
    r.setdefault("quality", {"method": "manual", "score": None})
    r.setdefault("quality_score", r["quality"].get("score"))
    r.setdefault("timestamp", r.get("created_at"))
    r.setdefault("latency", r.get("latency_seconds"))
    r.setdefault("cost", r.get("estimated_api_cost"))
    r.setdefault("estimated_api_cost", None)
    for field in COST_FIELDS:
        r.setdefault(field, None)
    if not r.get("cost_estimation_method"):
        r["cost_estimation_method"] = "legacy_frozen" if r["estimated_api_cost"] is not None else "unavailable"
        r["cost_unavailable_reason"] = "legacy_price_metadata_missing"
    r.setdefault("schema_version", 1)
    r.update(diagnose_record(r))
    return r
