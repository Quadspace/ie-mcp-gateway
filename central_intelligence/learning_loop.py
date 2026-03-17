#!/usr/bin/env python3
"""Daily Learning Loop — runs every day, analyzes telemetry, generates improvements."""

import sqlite3, json, os, sys
from pathlib import Path
from datetime import datetime, timedelta
import urllib.request

CI_DB = Path(os.environ.get('CI_DB_PATH', '~/.config/ie-mcp/central_intelligence.db')).expanduser()
CI_API = os.environ.get('CI_API_URL', 'http://localhost:8766')
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '')

def get_telemetry_summary():
    """Pull recent telemetry from the Central Intelligence API."""
    try:
        with urllib.request.urlopen(f'{CI_API}/api/telemetry/summary', timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[WARN] Could not reach CI API: {e}')
        return None

def get_local_outcomes():
    """Read local outcomes DB for detailed pattern analysis."""
    local_db = Path('~/.config/ie-mcp/gateway.db').expanduser()
    if not local_db.exists():
        return []
    conn = sqlite3.connect(str(local_db))
    try:
        rows = conn.execute("""
            SELECT task_type, status, duration_s, error_class, gateway_version, created_at
            FROM task_outcomes
            WHERE created_at > datetime('now', '-7 days')
            ORDER BY created_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(zip(['task_type','status','duration_s','error_class','gateway_version','created_at'], r)) for r in rows]

def analyze_with_gpt(outcomes, telemetry):
    """Use GPT-4 to identify patterns and generate improvement proposals."""
    if not OPENAI_KEY:
        print('[WARN] No OPENAI_API_KEY — using rule-based analysis')
        return rule_based_analysis(outcomes)

    summary = {
        'total_tasks': len(outcomes),
        'success_rate': round(sum(1 for o in outcomes if o['status'] == 'success') / max(len(outcomes), 1) * 100, 1),
        'avg_duration_s': round(sum(o['duration_s'] for o in outcomes) / max(len(outcomes), 1), 1),
        'error_classes': list(set(o['error_class'] for o in outcomes if o['error_class'])),
        'telemetry': telemetry,
    }

    prompt = f"""You are the IE.AI self-learning system. Analyze this gateway performance data and generate 1-3 concrete improvement proposals.

Data (last 7 days):
{json.dumps(summary, indent=2)}

For each improvement, output a JSON object with:
- title: short title
- description: what to improve and why
- skill_name: which skill file to update (e.g. 'claude-coder')
- skill_patch: the exact text to add/change in the skill (be specific and actionable)

Output ONLY a JSON array of improvement objects. No other text."""

    payload = json.dumps({
        'model': 'openai/gpt-4o-mini',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.3,
        'max_tokens': 1000,
    }).encode()

    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {OPENAI_KEY}', 'HTTP-Referer': 'https://industrialengineer.ai', 'X-Title': 'IE.AI Learning Loop'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    content = result['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('```')[1]
        if content.startswith('json'):
            content = content[4:]
    return json.loads(content)

def rule_based_analysis(outcomes):
    """Fallback: simple rule-based pattern detection."""
    improvements = []
    if not outcomes:
        return improvements
    failed = [o for o in outcomes if o['status'] != 'success']
    fail_rate = len(failed) / len(outcomes)
    if fail_rate > 0.2:
        improvements.append({
            'title': f'High failure rate detected: {fail_rate*100:.0f}%',
            'description': f'{len(failed)} of {len(outcomes)} tasks failed in the last 7 days. Review error classes: {list(set(o["error_class"] for o in failed if o["error_class"]))}',
            'skill_name': 'claude-coder',
            'skill_patch': 'Add retry logic for failed tasks. Check error class before retrying.'
        })
    slow = [o for o in outcomes if o['duration_s'] > 120]
    if len(slow) > 2:
        improvements.append({
            'title': f'{len(slow)} tasks exceeded 2 minutes',
            'description': 'Long-running tasks risk connection timeouts. Break large tasks into smaller surgical steps.',
            'skill_name': 'claude-coder',
            'skill_patch': 'Maximum task complexity: 3 files changed per call. Split larger changes.'
        })
    return improvements

def post_improvements(improvements):
    """Post improvement proposals to the Central Intelligence API."""
    posted = 0
    for imp in improvements:
        try:
            payload = json.dumps(imp).encode()
            req = urllib.request.Request(
                f'{CI_API}/api/improvements',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            urllib.request.urlopen(req, timeout=5)
            posted += 1
            print(f'  [+] Improvement posted: {imp["title"]}')
        except Exception as e:
            print(f'  [!] Failed to post improvement: {e}')
    return posted

def run():
    print(f'[{datetime.now().isoformat()}] IE.AI Daily Learning Loop starting...')
    outcomes = get_local_outcomes()
    print(f'  Loaded {len(outcomes)} task outcomes from last 7 days')
    telemetry = get_telemetry_summary()
    print(f'  Telemetry: {telemetry}')
    if not outcomes and not telemetry:
        print('  No data to analyze. Exiting.')
        return
    print('  Analyzing with GPT...')
    try:
        improvements = analyze_with_gpt(outcomes, telemetry)
        print(f'  Generated {len(improvements)} improvement proposals')
        posted = post_improvements(improvements)
        print(f'  Posted {posted} improvements to Central Intelligence API')
    except Exception as e:
        print(f'  [ERROR] Analysis failed: {e}')
        import traceback; traceback.print_exc()
    print(f'[{datetime.now().isoformat()}] Learning loop complete.')

if __name__ == '__main__':
    run()
