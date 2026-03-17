#!/usr/bin/env python3
"""Central Intelligence API - receives telemetry from all gateway instances,
stores patterns, and serves improvement recommendations."""

import sqlite3, json, os, hashlib
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

VERSION = '1.0.0'
DB_PATH = Path(os.environ.get('CI_DB_PATH', '~/.config/ie-mcp/central_intelligence.db')).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='IE.AI Central Intelligence', version=VERSION)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_s REAL NOT NULL,
            error_class TEXT DEFAULT '',
            gateway_version TEXT DEFAULT '',
            skill_version TEXT DEFAULT '',
            output_length INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            description TEXT NOT NULL,
            affected_instances INTEGER DEFAULT 0,
            improvement_applied INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            skill_name TEXT DEFAULT '',
            skill_patch TEXT DEFAULT '',
            applied_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

init_db()

@app.get('/health')
def health():
    conn = sqlite3.connect(str(DB_PATH))
    counts = conn.execute('SELECT (SELECT COUNT(*) FROM telemetry), (SELECT COUNT(*) FROM patterns), (SELECT COUNT(*) FROM improvements)').fetchone()
    conn.close()
    return {'status': 'ok', 'version': VERSION, 'service': 'IE.AI Central Intelligence',
            'telemetry_count': counts[0], 'pattern_count': counts[1], 'improvement_count': counts[2]}

@app.post('/api/telemetry')
async def receive_telemetry(request: Request):
    """Receive anonymized telemetry from a gateway instance."""
    try:
        data = await request.json()
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            'INSERT INTO telemetry (received_at, instance_id, task_type, status, duration_s, error_class, gateway_version, skill_version, output_length) VALUES (?,?,?,?,?,?,?,?,?)',
            (datetime.utcnow().isoformat(), data.get('instance_id','unknown'), data.get('task_type','unknown'),
             data.get('status','unknown'), float(data.get('duration_s', 0)), data.get('error_class',''),
             data.get('gateway_version',''), data.get('skill_version',''), int(data.get('output_length',0)))
        )
        conn.commit()
        conn.close()
        return {'received': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)

@app.get('/api/telemetry/summary')
def telemetry_summary():
    """Aggregate stats across all instances."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("""
        SELECT task_type, status, COUNT(*) as count,
               ROUND(AVG(duration_s),1) as avg_duration,
               COUNT(DISTINCT instance_id) as unique_instances
        FROM telemetry GROUP BY task_type, status ORDER BY count DESC
    """).fetchall()
    total = conn.execute('SELECT COUNT(*), COUNT(DISTINCT instance_id) FROM telemetry').fetchone()
    conn.close()
    return {'total_events': total[0], 'unique_instances': total[1],
            'breakdown': [dict(zip(['task_type','status','count','avg_duration','unique_instances'],r)) for r in rows]}

@app.get('/api/improvements')
def get_improvements():
    """Return available improvements for gateways to pull."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute('SELECT id, created_at, title, description, skill_name, skill_patch FROM improvements ORDER BY id DESC').fetchall()
    conn.close()
    cols = ['id','created_at','title','description','skill_name','skill_patch']
    return [dict(zip(cols, r)) for r in rows]

@app.post('/api/improvements')
async def create_improvement(request: Request):
    """Add a new improvement (called by the daily learning loop)."""
    data = await request.json()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        'INSERT INTO improvements (created_at, title, description, skill_name, skill_patch) VALUES (?,?,?,?,?)',
        (datetime.utcnow().isoformat(), data.get('title',''), data.get('description',''),
         data.get('skill_name',''), data.get('skill_patch',''))
    )
    conn.commit()
    conn.close()
    return {'created': True}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8766)
