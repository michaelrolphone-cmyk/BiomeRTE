"""Durable experiment requests. Restart requires explicit versioned resume."""
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from experiments.storage import canonical, digest


class ExperimentJobs:
    def __init__(self, root, manifest, runner=None):
        self.root = Path(root) / 'experiments'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manifest = str(manifest)
        self.runner = runner
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'jobs.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, version INTEGER, state TEXT, config TEXT, error TEXT); CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, digest TEXT, receipt TEXT);')
        with self.db:
            self.db.execute("UPDATE jobs SET state='interrupted', version=version+1 WHERE state IN ('queued','running')")

    def get(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(run_id)
            result = dict(run_id=row['id'], version=row['version'], status=row['state'], error=row['error'])
        progress = self.root / run_id / 'progress.json'
        result['progress'] = json.loads(progress.read_text()) if progress.exists() else None
        config=json.loads(row['config']);kind=config.get('kind','memory')
        report_path=self.root/run_id/({'memory':'report/report.json','stability':'stability.json','descriptors':'descriptor-report.json'}[kind])
        result['report']=None
        if report_path.exists():
            data=json.loads(report_path.read_text())
            if kind=='descriptors':
                result['report']={k:data[k] for k in ('label','generalization_scope','counts','heldout_auroc_target_met')}
                result['report']['methods']={name:dict(coherence=method['coherence'],retrieval_accuracy=method['cross_domain_retrieval']['accuracy']) for name,method in data['methods'].items()}
            elif kind=='stability':
                result['report']={k:data[k] for k in ('status','consumed_tokens','checkpoint_tokens','nll_relative_increase','accuracy_change','meets_duration','stability_target_met','scientific_memory_claim') if k in data}
            else:
                result['report']={k:data[k] for k in ('conditions','comparisons','completed_trials','scientific_success','reason')}
        result['next_action'] = 'Resume explicitly with a new request_id and current version' if row['state'] in ('failed', 'interrupted') else ('Review the measured report; this job is no longer running' if row['state'] in ('succeeded','partial') else 'Read this run_id for progress')
        return result

    def start(self, body):
        if set(body) - {'request_id', 'expected_version', 'config', 'run_id'}:
            raise ValueError('Unknown experiment request field')
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('request_id required')
        fingerprint = digest(body)
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise ValueError('Experiment request_id reused with changed content')
                return json.loads(old['receipt'])
            run_id = body.get('run_id')
            if run_id:
                row = self.db.execute('SELECT * FROM jobs WHERE id=?', (run_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(run_id)
                if type(body.get('expected_version')) is not int or body['expected_version'] != row['version'] or row['state'] not in ('failed', 'interrupted'):
                    raise ValueError('Stale experiment version or run is not resumable')
                config = json.loads(row['config'])
                if 'config' in body:
                    raise ValueError('Resume uses the frozen original configuration')
                version = row['version'] + 1
                self.db.execute("UPDATE jobs SET version=?,state='queued',error=NULL WHERE id=?", (version, run_id))
            else:
                if type(body.get('expected_version')) is not int or body['expected_version'] != 0:
                    raise ValueError('New experiment requires expected_version=0')
                if not isinstance(body.get('config'),dict):raise ValueError('config must be an object')
                config = dict(body['config'])
                if 'output' in config or 'manifest' in config:
                    raise ValueError('API experiments use the installed manifest and managed output directory')
                from experiments.evaluate import validate_config
                validate_config(config)
                run_id = uuid.uuid4().hex
                config.update(output=str(self.root / run_id), manifest=self.manifest)
                version = 0
                self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,NULL)', (run_id, version, 'queued', canonical(config)))
            receipt = dict(run_id=run_id, version=version, status='queued')
            self.db.execute('INSERT INTO requests VALUES(?,?,?)', (request_id, fingerprint, canonical(receipt)))
        threading.Thread(target=self._run, args=(run_id, config, bool(body.get('run_id'))), daemon=True).start()
        return receipt

    def _run(self, run_id, config, resume):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (run_id,))
        try:
            if self.runner is None:
                from experiments.evaluate import run
                run(config, resume)
            else:
                self.runner(config, resume)
            progress = Path(config['output']) / 'progress.json' if 'output' in config else None
            partial = progress is not None and progress.exists() and json.loads(progress.read_text()).get('status') == 'partial'
            state, error = ('partial' if partial else 'succeeded'), None
        except Exception as exc:
            state, error = 'failed', type(exc).__name__ + ': ' + str(exc)
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET state=?,error=?,version=version+1 WHERE id=?', (state, error, run_id))
