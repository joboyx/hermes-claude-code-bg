"""Live smoke suite for claude-code-bg: real `claude`, claude-haiku-5-5, effort low, isolated Hermes home.

Run it through `scripts/smoke/run.sh`, which builds the isolated home and cleans up after it.

  smoke.py [case ...]    run the cases (default: all, in order) and print a summary table
  smoke.py case <name>   run one in-process case in this process (the runner calls this)
  smoke.py cleanup       stop and remove every `smoke-` row of `claude agents`

In-process cases drive Hermes' own AIAgent + SessionDB (the CLI and gateway code path); each runs
in its own child process with its own config.yaml and environment. Gateway cases start one real
`hermes gateway run` with the loopback platform in `local_platform/` and talk to it over HTTP.
Every case prints PASS, FAIL or SKIP with its numbers; every case's Claude rows are removed, and a
case whose rows cannot be removed is a FAIL.

Every Claude turn uses claude-haiku-5-5, with one exception: without SMOKE_OTHER_PROVIDER the
/model case switches chat B to claude-sonnet-5-5 for one short turn ("Reply only: BETA-1.") and
switches it back to Haiku at once. The image and busy cases use their own chats, opted in to
claude-code-bg with Haiku, so they never inherit that switch.

Environment (run.sh sets the first three):
  HERMES_HOME                  the isolated home (required)
  SMOKE_CWD                    a directory Claude Code trusts; the conversations run there (required)
  SMOKE_OTHER_PROVIDER         a second provider installed in the home, for auxiliary routing and
                               the /model case; unset -> the auxiliary case is SKIP
  SMOKE_OTHER_MODEL            model of that provider (default: haiku)
  SMOKE_PORT                   loopback port of the gateway platform (default: 18797)
  SMOKE_MAX_LIVE               CLAUDE_CODE_BG_MAX_LIVE for the run (default: 8; other sessions on
                               the host count against it)
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zlib
from pathlib import Path

MODEL = 'claude-haiku-5-5'
PREFIX = 'smoke-'
PROVIDER = 'claude-code-bg'
REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / '.smoke'
HERMES_REPO = Path(os.environ.get('HERMES_AGENT_REPO') or Path.home() / '.hermes' / 'hermes-agent')
OTHER = os.environ.get('SMOKE_OTHER_PROVIDER') or None
OTHER_MODEL = os.environ.get('SMOKE_OTHER_MODEL') or 'haiku'
PORT = int(os.environ.get('SMOKE_PORT') or 18797)
OUTBOX = SMOKE / 'outbox.jsonl'
DROP_MARKERS = ('TELEGRAM', 'DISCORD', 'SLACK', 'WHATSAPP', 'MATRIX', 'MATTERMOST', 'SIGNAL')


def home() -> Path:
    return Path(os.environ['HERMES_HOME'])


def say(*parts) -> None:
    print(time.strftime('%H:%M:%S'), *parts, flush=True)


# ---------------------------------------------------------------- environment and config

def child_env(extra: dict | None = None) -> dict:
    """The environment of a clean host: no parent Claude Code session, no API keys or tokens
    (Hermes must not fall back to another paid route), no messaging-platform credentials."""
    env = {}
    for key, value in os.environ.items():
        if key.startswith('CLAUDE_CODE_') or key in ('CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT'):
            continue  # also drops CLAUDE_CODE_BG_*: the suite sets its own below
        if key.startswith('CLAUDE_BASH_') or key.endswith(('_API_KEY', '_TOKEN', '_SECRET')):
            continue
        if any(marker in key for marker in DROP_MARKERS):
            continue
        env[key] = value
    env.update({
        'HERMES_HOME': str(home()),
        'HERMES_SKIP_UPDATE_CHECK': '1',
        'CLAUDE_CODE_BG_CWD': os.environ['SMOKE_CWD'],
        'CLAUDE_CODE_BG_EFFORT': 'low',
        'CLAUDE_CODE_BG_NAME_PREFIX': PREFIX,
        'CLAUDE_CODE_BG_IDLE_STOP_S': '0',
        'CLAUDE_CODE_BG_MAX_LIVE': os.environ.get('SMOKE_MAX_LIVE') or '8',
    })
    env.update(extra or {})
    return env


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def write_config(over: dict | None = None) -> None:
    """Write `<home>/config.yaml` (JSON is valid YAML). Titles are off unless a case turns them on."""
    cfg = {
        'model': {'provider': PROVIDER, 'default': MODEL},
        'agent': {'reasoning_effort': 'low'},
        'providers': {PROVIDER: {'request_timeout_seconds': 900}},
        'auxiliary': {'title_generation': {'enabled': False}},
    }
    if OTHER:
        cfg['auxiliary']['compression'] = {'provider': OTHER, 'model': OTHER_MODEL}
    (home() / 'config.yaml').write_text(json.dumps(merge(cfg, over or {}), indent=2))


# ---------------------------------------------------------------- claude rows and events

def claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['claude', *args], env=child_env(), capture_output=True, text=True, timeout=60)


def agents() -> dict[str, dict]:
    out = claude('agents', '--json', '--all').stdout
    start = out.find('[')
    rows = json.loads(out[start:]) if start >= 0 else []
    return {r['id']: r for r in rows if isinstance(r, dict) and r.get('id')}


def pid_alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def smoke_rows() -> dict[str, dict]:
    return {k: v for k, v in agents().items() if (v.get('name') or '').startswith(PREFIX)}


def cleanup_rows() -> int:
    """Stop every `smoke-` row, wait for its process to exit, remove it; return how many are left."""
    rows = smoke_rows()
    for cid in rows:
        claude('stop', cid)
    for cid, row in rows.items():
        pid = row.get('pid')
        for _ in range(60):
            now = agents().get(cid)
            if not pid_alive(pid) and (now is None or now.get('status') is None):
                break
            time.sleep(0.5)
        claude('rm', cid)
    return len(smoke_rows())


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def events(conv: str | None = None, kind: str | None = None, since: float = 0.0) -> list[dict]:
    path = home() / 'claude-code-bg' / 'events.jsonl'
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('t', 0) < since or (conv and e.get('conv') != conv) or (kind and e.get('kind') != kind):
            continue
        out.append(e)
    return out


def conv_state(conv: str) -> dict:
    return json.loads((home() / 'claude-code-bg' / 'conv' / f'{conv}.json').read_text())


def cache_ratio(usage: dict) -> float:
    total = usage['cache_read'] + usage['cache_creation'] + usage['input']
    return round(usage['cache_read'] / total, 4) if total else 0.0


def wait_until(check, timeout: float, every: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(every)
    return check()


# ---------------------------------------------------------------- in-process Hermes

def hermes_path() -> None:
    if str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))


_DB = None


def session_db():
    global _DB
    if _DB is None:
        hermes_path()
        from hermes_state_registry import acquire
        _DB = acquire()
    return _DB


def profile():
    hermes_path()
    from providers import get_provider_profile
    found = get_provider_profile(PROVIDER)
    if found is None:
        raise RuntimeError(f'{PROVIDER} is not registered in {home()}')
    return found


class Chat:
    """One Hermes session driven through AIAgent.run_conversation, as the CLI does."""

    def __init__(self):
        hermes_path()
        from run_agent import AIAgent
        self.agent = AIAgent(provider=PROVIDER, model=MODEL, quiet_mode=True, platform='cli',
                             session_db=session_db())
        self.history: list = []

    @property
    def conv(self) -> str:
        return 'hs-' + sha16(self.agent.session_id)

    def turn(self, text: str, interrupt_after: float | None = None) -> dict:
        t0 = time.time()
        timer = threading.Timer(interrupt_after, self.agent.interrupt) if interrupt_after else None
        if timer:
            timer.start()
        r = self.agent.run_conversation(text, conversation_history=list(self.history))
        if timer:
            timer.cancel()
        self.history[:] = r.get('messages') or self.history
        out = {'reply': r.get('final_response') or '', 'interrupted': bool(r.get('interrupted')),
               'error': r.get('error'), 'wall_s': round(time.time() - t0, 2)}
        say('turn', json.dumps({'prompt': text[:60], **{k: (v[:200] if isinstance(v, str) else v)
                                                         for k, v in out.items()}}))
        return out


def sends(conv: str) -> list[dict]:
    return events(conv, 'send')


def ops(conv: str) -> list[str]:
    return [e['op'] for e in sends(conv)]


# ---------------------------------------------------------------- in-process cases

def case_usage():
    """`/usage`: plan windows from Claude Code's get_usage; no transcript turn, no inference."""
    projects = Path.home() / '.claude' / 'projects'

    def snapshot():
        return {p: p.stat().st_size for p in projects.glob('*/*.jsonl')}

    dirs = set(projects.iterdir())
    before = snapshot()
    t0 = time.time()
    snap = profile().fetch_account_usage()
    took = round(time.time() - t0, 2)
    after = snapshot()
    sdk_assistant = 0
    for path, size in after.items():
        start = before.get(path, 0)
        if size <= start:
            continue
        with open(path, 'rb') as fh:
            fh.seek(start)
            for line in fh.read().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get('entrypoint') == 'sdk-cli' and e.get('type') == 'assistant':
                    sdk_assistant += 1
    new_dirs = [d.name for d in set(projects.iterdir()) - dirs if 'usage' in d.name]
    windows = [f'{w.label} {w.used_percent:.0f}%' if w.used_percent is not None else w.label
               for w in snap.windows]
    ok = bool(windows) and snap.unavailable_reason is None and sdk_assistant == 0 and not new_dirs
    return ok, {'windows': windows, 'seconds': took, 'reason': snap.unavailable_reason,
                'sdk_cli_assistant_entries': sdk_assistant, 'new_usage_project_dirs': new_dirs}


def case_single():
    c = Chat()
    r = c.turn('Reply only: SMOKE-ONE.')
    o = ops(c.conv)
    return 'SMOKE-ONE' in r['reply'] and o == ['dispatch'], {'wall_s': r['wall_s'], 'ops': o}


def case_recall():
    c = Chat()
    c.turn('Remember the code word KIWI-42. Reply only: ok.')
    c.turn('Reply only: two.')
    r = c.turn('What was the code word? Reply with it only.')
    s = sends(c.conv)
    sessions = {e['session_id'] for e in s}
    usage = events(c.conv, 'done')[-1]['usage']
    ratio = cache_ratio(usage)
    ok = ('KIWI-42' in r['reply'] and len(sessions) == 1
          and [e['op'] for e in s] == ['dispatch', 'follow-up', 'follow-up'] and ratio >= 0.95)
    return ok, {'turn3_cache_ratio': ratio, 'claude_sessions': len(sessions), 'ops': [e['op'] for e in s],
                'turn3_usage': {k: usage[k] for k in ('input', 'cache_read', 'cache_creation', 'output')}}


def case_two_sessions():
    a, b = Chat(), Chat()
    ra = a.turn('Reply only: SESSION-A.')
    rb = b.turn('Reply only: SESSION-B.')
    sa, sb = sends(a.conv), sends(b.conv)
    sessions = {e['session_id'] for e in sa + sb}
    ok = ('SESSION-A' in ra['reply'] and 'SESSION-B' in rb['reply'] and a.conv != b.conv
          and [e['op'] for e in sa] == ['dispatch'] and [e['op'] for e in sb] == ['dispatch']
          and len(sessions) == 2)
    return ok, {'hermes_sessions': 2, 'claude_sessions': len(sessions)}


def case_cancel():
    c = Chat()
    r1 = c.turn('Write a 1000-word story about a fox. Use no tools. End with a line that says only: FOX-END',
                interrupt_after=10)
    r2 = c.turn('Reply only: AFTER-CANCEL.')
    s = sends(c.conv)
    orphans = [e['state'] for e in events(c.conv, 'orphan')]
    ok = r1['interrupted'] and 'AFTER-CANCEL' in r2['reply'] and len({e['session_id'] for e in s}) == 1
    return ok, {'cancel_returned_s': r1['wall_s'], 'interrupted': r1['interrupted'],
                'continue_s': r2['wall_s'], 'ops': [e['op'] for e in s], 'orphans': orphans,
                'late_reply_in_continue': 'FOX-END' in r2['reply']}


def case_fault():
    c = Chat()
    c.turn('Reply only: F-ONE.')
    os.environ['CLAUDE_CODE_BG_FAULT'] = 'after_send'
    try:
        r = c.turn('Reply only: FAULT-OK.')
    finally:
        os.environ.pop('CLAUDE_CODE_BG_FAULT', None)
    faults = events(c.conv, 'fault_injected')
    turn = faults[0]['turn'] if faults else None
    follow = [e for e in sends(c.conv) if e['turn'] == turn]
    dedupe = [e['state'] for e in events(c.conv, 'dedupe') if e['turn'] == turn]
    ok = ('FAULT-OK' in r['reply'] and len(faults) == 1 and len(follow) == 1
          and follow[0]['op'] in ('follow-up', 'copy') and dedupe)
    return ok, {'faults': len(faults), 'follow_ups_for_turn': len(follow), 'dedupe': dedupe,
                'wall_s': r['wall_s']}


def case_watchdog():
    # Claude Code blocks a long foreground sleep; a background command keeps the turn open
    # (the job's in-flight task) until it ends, about 45 s.
    c = Chat()
    r = c.turn('Run the shell command `sleep 45; echo slept` with your Bash tool with run_in_background '
               'set to true. Wait until it has finished, then reply only: WD-DONE.')
    dedupe = [e['state'] for e in events(c.conv, 'dedupe')]
    o = ops(c.conv)
    ok = 'WD-DONE' in r['reply'] and o == ['dispatch'] and 'sent' in dedupe and r['wall_s'] >= 40
    return ok, {'wall_s': r['wall_s'], 'ops': o, 'dedupe': dedupe}


def case_reap():
    # The first resume of any session writes ~14K cache tokens again (Claude Code, also without a
    # reap or persona); turn 2 takes that cost, so turn 3 shows what the reap itself costs.
    c = Chat()
    c.turn('Remember the code word PLUM-3. Reply only: ok.')
    c.turn('Reply only: two.')
    cid = conv_state(c.conv)['claude_id']
    t0 = time.time()
    reaped = wait_until(lambda: [e for e in events(c.conv, 'reaped') if e.get('by') == 'reaper'], 150, 2)
    reaped_after = round(time.time() - t0, 1)
    row = agents().get(cid) or {}
    exited = wait_until(lambda: not pid_alive(row.get('pid')), 30, 1)
    r = c.turn('What was the code word? Reply with it only.')
    s = sends(c.conv)
    usage = events(c.conv, 'done')[-1]['usage']
    ratio = cache_ratio(usage)
    ok = (bool(reaped) and exited and 'PLUM-3' in r['reply'] and len({e['session_id'] for e in s}) == 1
          and s[-1]['op'] == 'follow-up' and ratio >= 0.95)
    return ok, {'reaped': bool(reaped), 'reaped_after_s': reaped_after,
                'idle_s': reaped[0].get('idle_s') if reaped else None, 'process_exited': bool(exited),
                'resume_cache_ratio': ratio, 'ops': [e['op'] for e in s],
                'resume_usage': {k: usage[k] for k in ('input', 'cache_read', 'cache_creation')}}


def case_retry_after_reap():
    """A Hermes retry of a timed-out turn after the reaper stopped the idle process: does the
    wait end promptly (the reply is in the transcript) or run to the request deadline?"""
    client = profile().create_client()
    sid = f'smoke-retry-{int(time.time())}'
    conv = 'hs-' + sha16(sid)
    msgs = [{'role': 'system', 'content': 'smoke'},
            {'role': 'user', 'content': 'Write a 250-word story about a heron. Use no tools. '
                                        'End with a line that says only: HERON-END'}]
    kw = {'model': MODEL, 'messages': msgs, 'hermes_session_id': sid, 'hermes_reasoning_effort': 'low'}
    try:
        client.chat.completions.create(timeout=3, **kw)
        first = 'answered'
    except TimeoutError:
        first = 'timeout'
    say('first call:', first)
    t0 = time.time()
    reaped = wait_until(lambda: [e for e in events(conv, 'reaped') if e.get('by') == 'reaper'], 150, 2)
    reaped_after = round(time.time() - t0, 1)
    t1 = time.time()
    r = client.chat.completions.create(timeout=300, **kw)
    retry_s = round(time.time() - t1, 2)
    text = r.choices[0].message.content
    dedupe = [e['state'] for e in events(conv, 'dedupe')]
    o = ops(conv)
    ok = first == 'timeout' and bool(reaped) and 'HERON-END' in text and o == ['dispatch'] and retry_s < 20
    return ok, {'first_call': first, 'reaped_after_s': reaped_after, 'retry_s': retry_s, 'ops': o,
                'dedupe': dedupe, 'reply_tail': text[-40:]}


def case_aux_title():
    if not OTHER:
        return None, {'reason': 'no second provider installed (set SMOKE_OTHER_PROVIDER_PLUGIN in run.sh)'}
    t0 = time.time()
    c = Chat()
    r = c.turn('Help me plan a weekend hiking trip to the mountains with my sister. '
               'For now reply only: TITLE-OK.')
    db = session_db()
    sid = c.agent.session_id
    src = wait_until(lambda: db.get_session_title_source(sid) == 'llm', 90, 2)
    refused = events(kind='aux_refused', since=t0)
    ok = bool(src) and 'TITLE-OK' in r['reply'] and not refused
    return ok, {'title': db.get_session_title(sid), 'title_source': db.get_session_title_source(sid),
                'title_s': round(time.time() - t0, 1), 'aux_refused_events': len(refused),
                'aux_provider': OTHER}


# ---------------------------------------------------------------- gateway cases

def post(chat: str, text: str, media: list | None = None) -> float:
    body = json.dumps({'chat_id': chat, 'text': text, 'media': media or []}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/msg', data=body,
                                 headers={'Content-Type': 'application/json'})
    t = time.time()
    urllib.request.urlopen(req, timeout=10).read()
    say('post', chat, text[:70])
    return t


def outbox(chat: str, since: float) -> list[dict]:
    rows = []
    for line in OUTBOX.read_text().splitlines() if OUTBOX.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('t', 0) >= since and r.get('chat_id') == chat and r.get('event') in ('send', 'edit'):
            rows.append(r)
    return rows


def wait_reply(chat: str, since: float, match, timeout: float = 150) -> dict | None:
    found = wait_until(lambda: next((r for r in outbox(chat, since) if match(r.get('content') or '')), None),
                       timeout, 0.5)
    if found is None:
        say('no match; outbox:', json.dumps([(r.get('content') or '')[:120] for r in outbox(chat, since)]))
    else:
        say('reply', chat, json.dumps((found.get('content') or '')[:200]))
    return found


def sends_with(token: str, since: float) -> list[dict]:
    """claude-code-bg `send` events since `since` whose full prompt (the text Claude Code got,
    the /model note already stripped) contains `token`. The event keeps only 90 characters of
    the prompt, so the full text comes from the turn record in the conversation state."""
    turns = set()
    for conv in (home() / 'claude-code-bg' / 'conv').glob('*.json'):
        for key, turn in json.loads(conv.read_text()).get('turns', {}).items():
            if (turn.get('sent_at') or 0) >= since and token in (turn.get('prompt') or ''):
                turns.add((conv.stem, key))
    return [e for e in events(kind='send', since=since) if (e.get('conv'), e.get('turn')) in turns]


def opt_in(chat: str, model: str = MODEL) -> dict | None:
    """Switch `chat` to claude-code-bg with `model` through the /model command."""
    return wait_reply(chat, post(chat, f'/model {model} --provider {PROVIDER}'), lambda c: model in c, 60)


def case_model():
    """`/model` per chat: chat B switches to claude-code-bg (or, without a second provider, to
    Sonnet for one short turn and back to Haiku), chat A stays on the default."""
    t0 = time.time()
    a, b = 'smoke-chat-a', 'smoke-chat-b'
    target = MODEL if OTHER else 'claude-sonnet-5-5'
    ra = wait_reply(a, post(a, 'Reply only: ALPHA-1.'), lambda c: 'ALPHA-1' in c)
    rm = opt_in(b, target)
    rb = wait_reply(b, post(b, 'Reply only: BETA-1.'), lambda c: 'BETA-1' in c)
    back = opt_in(b) if not OTHER else True  # the one documented Sonnet turn ends here
    ra2 = wait_reply(a, post(a, 'Reply only: ALPHA-2.'), lambda c: 'ALPHA-2' in c)
    a_reqs, b_reqs = sends_with('ALPHA', t0), sends_with('BETA', t0)
    if OTHER:
        routed = not a_reqs and bool(b_reqs) and all(e['model'] == MODEL for e in b_reqs)
    else:
        routed = (bool(a_reqs) and all(e['model'] == MODEL for e in a_reqs)
                  and bool(b_reqs) and all(e['model'] == target for e in b_reqs))
    ok = all((ra, rm, rb, back, ra2)) and routed
    return ok, {'chat_a_default': OTHER or f'{PROVIDER}/{MODEL}', 'chat_b': f'{PROVIDER}/{target}',
                'chat_b_back_to_haiku': bool(back) if not OTHER else None,
                'chat_a_ccbg_sends': len(a_reqs), 'chat_b_ccbg_sends': len(b_reqs),
                'chat_b_models': sorted({e['model'] for e in b_reqs})}


def png(path: Path, rgb: tuple[int, int, int]) -> None:
    """Write a 64x64 solid-colour PNG."""
    raw = b''.join(b'\x00' + bytes(rgb) * 64 for _ in range(64))

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def case_image():
    """An image attachment reaches Claude Code as a saved file and the colour comes back."""
    chat = 'smoke-chat-image'
    question = 'What single colour fills this image? Reply with the colour name only.'
    t0 = time.time()
    opted = opt_in(chat)
    image = SMOKE / 'red.png'
    png(image, (255, 0, 0))
    saved = Path(os.environ['SMOKE_CWD']) / 'tmp' / 'hermes-images'
    before = set(saved.glob('*')) if saved.is_dir() else set()
    t = post(chat, question, media=[str(image)])
    r = wait_reply(chat, t, lambda c: 'red' in c.lower(), 120)
    new = (set(saved.glob('*')) if saved.is_dir() else set()) - before
    for path in new:
        path.unlink()  # the copy the client saved for Claude Code's Read tool
    sent = sends_with(question, t0)
    ok = bool(opted) and r is not None and len(new) >= 1 and bool(sent) and all(e['model'] == MODEL for e in sent)
    return ok, {'reply_s': round(r['t'] - t, 1) if r else None, 'saved_images': len(new),
                'ccbg_sends': len(sent), 'models': sorted({e['model'] for e in sent}),
                'reply': (r.get('content') or '')[:40] if r else None}


def case_busy():
    """A second message while the first turn runs: the second reply arrives from claude-code-bg."""
    chat = 'smoke-chat-busy'
    t0 = time.time()
    opted = opt_in(chat)
    t1 = post(chat, 'Run the shell command `sleep 20; echo first-done` with your Bash tool in the foreground, '
                    'then reply only: FIRST-DONE.')
    time.sleep(6)
    t2 = post(chat, 'Reply only: SECOND-DONE.')
    early = [x for x in outbox(chat, t1) if x['t'] < t2 and 'FIRST-DONE' in (x.get('content') or '')]
    r = wait_reply(chat, t2, lambda c: 'SECOND-DONE' in c, 200)
    sent = sends_with('SECOND-DONE', t0)
    first = [x for x in outbox(chat, t1) if 'FIRST-DONE' in (x.get('content') or '')]
    ok = (bool(opted) and r is not None and not early and bool(sent)
          and all(e['model'] == MODEL for e in sent))
    return ok, {'second_reply_s': round(r['t'] - t2, 1) if r else None, 'ccbg_sends_second': len(sent),
                'first_done_before_second': len(early), 'first_reply_seen': bool(first),
                'sends': len(outbox(chat, t1))}



IN_PROCESS = {
    'usage': (case_usage, {}, {}, 60),
    'single': (case_single, {}, {}, 300),
    'recall': (case_recall, {}, {}, 300),
    'two-sessions': (case_two_sessions, {}, {}, 300),
    'cancel': (case_cancel, {}, {}, 300),
    'fault': (case_fault, {}, {}, 300),
    'watchdog': (case_watchdog, {'providers': {PROVIDER: {'stale_timeout_seconds': 20}}}, {}, 400),
    'reap': (case_reap, {}, {'CLAUDE_CODE_BG_IDLE_STOP_S': '30'}, 400),
    'retry-after-reap': (case_retry_after_reap, {}, {'CLAUDE_CODE_BG_IDLE_STOP_S': '15'}, 400),
    'aux-title': (case_aux_title, {'auxiliary': {'title_generation': {
        'enabled': True, 'provider': OTHER, 'model': OTHER_MODEL}}} if OTHER else {}, {}, 300),
}
GATEWAY = {'model': case_model, 'image': case_image, 'busy': case_busy}
ORDER = list(IN_PROCESS) + list(GATEWAY)


# ---------------------------------------------------------------- runner

def run_child(name: str, run_dir: Path) -> dict:
    _, cfg, env, timeout = IN_PROCESS[name]
    write_config(cfg)
    log = run_dir / f'{name}.log'
    try:
        with open(log, 'w') as fh:
            subprocess.run([sys.executable, __file__, 'case', name], env=child_env(env), stdout=fh,
                           stderr=subprocess.STDOUT, cwd=SMOKE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'case': name, 'status': 'FAIL', 'numbers': {'error': f'no result in {timeout} s'}}
    lines = [x for x in log.read_text().splitlines() if x.startswith('RESULT ')]
    if not lines:
        return {'case': name, 'status': 'FAIL', 'numbers': {'error': f'no RESULT line, see {log.name}'}}
    return json.loads(lines[-1][len('RESULT '):])


def child(name: str) -> None:
    func = IN_PROCESS[name][0]
    try:
        ok, numbers = func()
        status = 'SKIP' if ok is None else ('PASS' if ok else 'FAIL')
    except Exception as exc:  # report the case as FAIL with the reason, then exit
        traceback.print_exc()
        status, numbers = 'FAIL', {'error': f'{type(exc).__name__}: {exc}'[:400]}
    print('RESULT ' + json.dumps({'case': name, 'status': status, 'numbers': numbers}, default=str), flush=True)
    os._exit(0)  # Hermes leaves non-daemon threads behind


def start_gateway(run_dir: Path) -> subprocess.Popen:
    over = {'plugins': {'enabled': ['smoke-local-platform']}}
    if OTHER:
        over['model'] = {'provider': OTHER, 'default': OTHER_MODEL}
    write_config(over)
    (SMOKE / 'locks').mkdir(exist_ok=True)
    env = child_env({'SMOKE_LOCAL_PORT': str(PORT), 'SMOKE_LOCAL_OUTBOX': str(OUTBOX),
                     'SMOKE_LOCAL_ALLOW_ALL_USERS': 'true', 'HERMES_GATEWAY_LOCK_DIR': str(SMOKE / 'locks')})
    hermes = Path(sys.executable).parent / 'hermes'
    t0 = time.time()
    proc = subprocess.Popen([str(hermes), 'gateway', 'run', '-v'], env=env, cwd=SMOKE,
                            stdout=open(run_dir / 'gateway.log', 'w'), stderr=subprocess.STDOUT,
                            start_new_session=True)

    def connected():
        if not OUTBOX.exists():
            return False
        return any(json.loads(x).get('event') == 'connected' and json.loads(x).get('t', 0) >= t0
                   for x in OUTBOX.read_text().splitlines() if x.strip())

    if not wait_until(connected, 120, 1):
        stop_gateway(proc)
        raise RuntimeError('gateway did not connect the smoke platform in 120 s (see gateway.log)')
    say('gateway up, port', PORT)
    return proc


def stop_gateway(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def report(results: list[dict]) -> None:
    print('\n| case | status | numbers |\n| --- | --- | --- |')
    for r in results:
        print(f"| {r['case']} | {r['status']} | {json.dumps(r['numbers'], default=str)} |")


def main(argv: list[str]) -> int:
    if argv[:1] == ['case']:
        child(argv[1])
        return 0
    if argv[:1] == ['cleanup']:
        left = cleanup_rows()
        say('smoke rows left:', left)
        return 0 if left == 0 else 1
    names = argv or ORDER
    unknown = [n for n in names if n not in ORDER]
    if unknown:
        raise SystemExit(f'unknown case(s): {unknown}; known: {ORDER}')
    run_dir = SMOKE / 'logs' / time.strftime('%Y%m%d-%H%M%S')
    run_dir.mkdir(parents=True)
    say('logs in', run_dir)
    results = []
    for name in [n for n in names if n in IN_PROCESS]:
        say('case', name)
        result = run_child(name, run_dir)
        result['numbers']['rows_left'] = left = cleanup_rows()
        if left:
            result['status'] = 'FAIL'  # every case must leave no smoke- row
        say(result['status'], name, json.dumps(result['numbers'], default=str))
        results.append(result)
    gateway_cases = [n for n in names if n in GATEWAY]
    if gateway_cases:
        proc = start_gateway(run_dir)
        try:
            for name in gateway_cases:
                say('case', name)
                try:
                    ok, numbers = GATEWAY[name]()
                    status = 'PASS' if ok else 'FAIL'
                except Exception as exc:  # report the case as FAIL with the reason, go on
                    traceback.print_exc()
                    status, numbers = 'FAIL', {'error': f'{type(exc).__name__}: {exc}'[:400]}
                say(status, name, json.dumps(numbers, default=str))
                results.append({'case': name, 'status': status, 'numbers': numbers})
        finally:
            stop_gateway(proc)
            left = cleanup_rows()
            for r in results[-len(gateway_cases):]:
                r['numbers']['rows_left'] = left
                if left:
                    r['status'] = 'FAIL'  # the gateway cases share one cleanup
    (run_dir / 'summary.json').write_text(json.dumps(results, indent=2, default=str))
    report(results)
    return 1 if any(r['status'] == 'FAIL' for r in results) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
