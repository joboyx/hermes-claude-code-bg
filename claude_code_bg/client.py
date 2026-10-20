"""OpenAI-shaped client that Hermes calls: one Claude Code background session per Hermes chat.

Hermes calls `chat.completions.create(**kwargs)` on a worker thread and `cancel()` from its
interrupt or stale-call watchdog thread. Each call is one turn: the first turn of a
conversation dispatches a `claude --bg` session, later turns send a follow-up to the same
session, and the reply is read from the session transcript. Only the newest user message goes
to Claude Code, plus a quoted block of the messages the Claude session has not seen (the chat
ran on another provider first, or in between); the Claude session keeps its own history. The
response never carries `tool_calls`, so Hermes ends its turn.

This module keeps only the turn orchestration and the response objects. The CLI calls are in
`driver`, the completion rule in `transcript`, keys and prompts in `messages`, state and locks
in `store`, and the host caps in `admission`.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from claude_code_bg import admission, catalog, driver, messages, store, transcript

CANCELLED = 'claude-code-bg request cancelled'
DENY_GUIDANCE = ('Not allowed in chat mode: no one can approve this prompt. Choose a safer way, '
                 'or explain in your reply what you need and stop.')
LATE_PREFIX = '(Reply to an earlier message that timed out:)'
DENIED_NOTE = ('(Claude Code hit a permission prompt in this turn. It was denied: no one can '
               'approve it in chat mode.)')
STOPPED_REPLY = ('Claude Code stopped this turn: it hit a permission prompt that no one can '
                 'approve in chat mode, and the prompt could not be denied here. Ask again in a '
                 'way that needs no approval.')
DEFAULT_TIMEOUT_S = 1800.0
INFLIGHT_NOTE = '(Background work is still running in this session.)'
EMPTY_REPLY = '(Claude finished without a text reply.)'
DEFAULT_INFLIGHT_GRACE_S = 120.0
POLL_S = 0.5
ROW_CHECK_POLLS = 20  # x POLL_S: read the agents row about every 10 s
EVENT_TEXT = 90  # never log more message text than this
ZERO_USAGE = {'input': 0, 'cache_read': 0, 'cache_creation': 0, 'output': 0, 'api_calls': 0,
              'last': {'input': 0, 'cache_read': 0, 'cache_creation': 0}}


class AuxiliaryRefused(RuntimeError):
    """
    An auxiliary call (title, compression, vision, approval ...) reached this provider.
    Set `auxiliary.<task>.provider` to another provider.
    """


class Object(SimpleNamespace):
    """
    A response object: attribute access plus `model_dump()`, the two shapes Hermes reads.
    """

    def model_dump(self, **_) -> dict:
        """
        Return the object tree as plain dicts and lists (deep copies of the leaves).
        """
        def unpack(value):
            if isinstance(value, Object):
                return {k: unpack(v) for k, v in vars(value).items()}
            if isinstance(value, list):
                return [unpack(v) for v in value]
            return copy.deepcopy(value)
        return unpack(self)


def obj(value):
    """
    Return `value` with every nested dict turned into an `Object` (lists are walked).
    """
    if isinstance(value, dict):
        return Object(**{k: obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


class _Request:
    """One in-flight `create` call; `cancel()` comes from another thread."""

    def __init__(self):
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError(CANCELLED)


def _cut(text: str) -> str:
    return text[:EVENT_TEXT]


def _session_name(conv: str) -> str:
    # The key starts with `hs-` or `hx-`; the 10 chars after it name the session.
    return (os.environ.get('CLAUDE_CODE_BG_NAME_PREFIX') or 'hb-') + conv[3:13]


def _timeout_of(value, default: float | None) -> float:
    def number(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0
    if number(value):
        return float(value)
    return float(default) if number(default) else DEFAULT_TIMEOUT_S


def inflight_grace_seconds() -> float:
    """
    Return how long an end_turn with text may stay held back only by in-flight background
    work before the reply is returned anyway: CLAUDE_CODE_BG_INFLIGHT_GRACE_S, else 120.
    """
    raw = os.environ.get('CLAUDE_CODE_BG_INFLIGHT_GRACE_S')
    return max(0.0, float(raw)) if raw else DEFAULT_INFLIGHT_GRACE_S


def _row_finished(row: dict) -> bool:
    """True when the agents row shows the process is no longer running a turn."""
    return row.get('state') in ('done', 'stopped') and row.get('status') in (None, 'idle')


class Client:
    """
    The object Hermes gets from the provider profile. Hermes reads `api_key`, `base_url`,
    `chat.completions.create` and the class flags below. `SUPPORTS_HERMES_TOOL_CALLS = False`
    makes Hermes skip its background-review fork, which would otherwise reuse the session id
    and land its review prompt in the user's Claude session.
    """

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True
    SUPPORTS_HERMES_TOOL_CALLS = False

    def __init__(self, command=None, args=None, timeout=None, **_):
        """
        Create the client. `command` and `args` (from the Hermes profile) are accepted and
        ignored: the driver runs CLAUDE_CODE_BG_COMMAND or `claude`. `timeout` is the default
        request timeout in seconds (else 1800). Starts the idle reaper when
        `admission.idle_stop_seconds()` is above 0.
        """
        self.api_key = 'external-process'
        self.base_url = 'process://claude-code-bg'
        self.timeout = timeout
        self._lock = threading.Lock()
        self._requests: set[_Request] = set()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        if admission.idle_stop_seconds() > 0:
            admission.start_reaper()

    def cancel(self) -> None:
        """
        Stop waiting in every in-flight `create` (they raise RuntimeError(CANCELLED)). Safe
        from any thread. The background turn keeps running; the next turn settles it.
        """
        with self._lock:
            requests = tuple(self._requests)
        for request in requests:
            request.cancelled.set()

    def close(self) -> None:
        """
        Same as `cancel()`; the client holds no other resources.
        """
        self.cancel()

    def create(self, **kwargs):
        """
        Answer one Hermes turn and return a chat.completion `Object`. Runs synchronously,
        except when the direct caller is a coroutine inside a running event loop: then it
        returns `asyncio.to_thread(...)` for the caller to await.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._create(**kwargs)
        flags = sys._getframe(1).f_code.co_flags
        if flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE):
            return asyncio.to_thread(self._create, **kwargs)
        return self._create(**kwargs)

    def _create(self, **kwargs):
        request = _Request()
        with self._lock:
            self._requests.add(request)
        try:
            return self._run(request, kwargs)
        finally:
            with self._lock:
                self._requests.discard(request)

    def _run(self, request: _Request, kwargs: dict):
        started = time.time()
        history = kwargs.get('messages') or []
        if kwargs.pop('hermes_auxiliary', False):
            store.event({'kind': 'aux_refused', 'model': kwargs.get('model'),
                         'n_messages': len(history)})
            raise AuxiliaryRefused('claude-code-bg does not serve auxiliary calls; set '
                                   'auxiliary.<task>.provider to another provider')
        hermes_session = kwargs.pop('hermes_session_id', None)
        effort = catalog.effort_of(kwargs.pop('hermes_reasoning_effort', None))
        model = kwargs.get('model') or catalog.DEFAULT_MODEL
        _, n_user = messages.newest_user(history)
        conv = messages.conversation_key(hermes_session, history)
        tkey = messages.turn_key(history)
        dicts = [m for m in history if isinstance(m, dict)]
        store.event({'kind': 'request', 'conv': conv, 'turn': tkey,
                     'hermes_session': hermes_session, 'n_user': n_user, 'model': model,
                     'effort': effort, 'roles': [m.get('role') for m in dicts],
                     'heads': [_cut(messages.text_of(m.get('content'))) for m in dicts
                               if m.get('role') != 'system'][-6:],
                     'n_tools': len(kwargs.get('tools') or []),
                     'keys': sorted(k for k in kwargs if k not in ('messages', 'tools'))})
        deadline = time.monotonic() + _timeout_of(kwargs.get('timeout'), self.timeout)
        with store.thread_lock(conv), store.FileLock(conv):
            return self._turn(request, conv, tkey, history, n_user, model, effort,
                              hermes_session, deadline, started)

    def _turn(self, request, conv, tkey, history, n_user, model, effort, hermes_session,
              deadline, started):
        """Steps 3-9 of the turn flow, under the conversation's thread and file locks."""
        state = store.load(conv)
        if hermes_session:
            state.setdefault('hermes_session', hermes_session)
        turn = state['turns'].get(tkey)
        if turn and turn.get('state') == 'done' and turn.get('delivered'):
            # The reply already reached the caller: the same list again is Hermes `/retry` (or
            # `/undo` + the same text), which asks for a new answer. Keep the old record under
            # its own key and send the message again.
            state['turns'][f'{tkey}~{time.time_ns()}'] = state['turns'].pop(tkey)
            store.event({'kind': 'regenerate', 'conv': conv, 'turn': tkey})
            turn = None
        if turn and turn.get('state') == 'done':
            # The earlier call finished the turn but raised or was cancelled before it returned.
            store.event({'kind': 'dedupe', 'conv': conv, 'turn': tkey, 'state': 'done'})
            return self._deliver(request, conv, state, model, tkey, turn)
        if turn and turn.get('state') == 'sent':
            # A Hermes retry of a turn already sent: wait for it, never send it again.
            store.event({'kind': 'dedupe', 'conv': conv, 'turn': tkey, 'state': 'sent'})
        else:
            verdict = admission.admit(conv)
            if not verdict.ok:
                return self._reply(model, f'ccbg-busy-{tkey}', admission.busy_reply(verdict),
                                   ZERO_USAGE)
            late = self._collect_orphans(state, conv, tkey)
            if late:
                state['late_replies'] = state.get('late_replies', []) + late
            turn = self._send(state, conv, tkey, history, n_user, model, effort, hermes_session)
            state['turns'][tkey] = turn
            store.save(conv, state)
            self._fault('after_send', conv, tkey)
        request.check()
        result = self._wait(request, conv, turn, deadline)
        late = state.pop('late_replies', None)
        if late:
            # Turns Hermes gave up on finished afterwards: deliver them ahead of this reply, once.
            result['text'] = '\n\n'.join([f'{LATE_PREFIX}\n{t}' for t in late] + [result['text']])
            result['late_delivered'] = len(late)
        turn.update(result, state='done', wall_s=round(time.time() - started, 2))
        state['last_done_at'] = time.time()
        store.event({'kind': 'done', 'conv': conv, 'turn': tkey, 'claude_id': turn['claude_id'],
                     'session_id': turn['session_id'], 'timing': turn.get('timing'),
                     'usage': turn['usage'], 'n_end_turns': turn.get('n_end_turns'),
                     'denied': turn.get('denied', False)})
        return self._deliver(request, conv, state, model, tkey, turn)

    def _deliver(self, request: _Request, conv: str, state: dict, model: str, tkey: str,
                 turn: dict) -> Object:
        """
        Return the response of a `done` turn and mark the turn `delivered`. A call cancelled
        meanwhile raises instead and leaves the turn undelivered, so the retry gets it.
        """
        if request.cancelled.is_set():
            store.save(conv, state)
            request.check()
        turn['delivered'] = True
        store.save(conv, state)
        return self._response(model, tkey, turn)

    def _read(self, turn: dict) -> transcript.Turn | None:
        return transcript.read_turn(turn['session_id'], turn['claude_id'], turn['offset'],
                                    turn['prompt'], skip_matches=turn.get('skip', 0))

    def _inspect(self, turn: dict) -> transcript.Turn | None:
        return transcript.inspect_turn(turn['session_id'], turn['claude_id'], turn['offset'],
                                       turn['prompt'], skip_matches=turn.get('skip', 0))

    def _collect_orphans(self, state: dict, conv: str, tkey: str) -> list[str]:
        """
        Read every other turn still `sent` (Hermes gave up on it) once: a finished one is
        marked `done` and its text returned for late delivery; an unfinished one is marked
        `abandoned` (the follow-up stops its process).
        """
        late = []
        for key, t in state['turns'].items():
            if key == tkey or t.get('state') != 'sent':
                continue
            done = self._read(t)
            if done is not None:
                t.update(state='done', text=done.text, usage=done.usage,
                         n_end_turns=done.n_end_turns, delivered_late=True)
                late.append(done.text)
            else:
                t['state'] = 'abandoned'
            store.event({'kind': 'orphan', 'conv': conv, 'turn': key, 'state': t['state']})
        return late

    def _send(self, state, conv, tkey, history, n_user, model, effort, hermes_session) -> dict:
        """
        Dispatch on the first turn, else follow up. Updates `state` with the session and
        the seen marker, and returns the `sent` turn record: where to read the reply
        (session, byte offset, number of earlier prompt matches to skip) and the exact prompt
        sent. The prompt is the newest user message; messages of this chat the Claude session
        has not seen (the chat ran on another provider) go before it as a quoted block.
        """
        cwd = state.get('cwd') or store.default_cwd()
        user_msg, _ = messages.newest_user(history)
        newest = messages.prompt_of(user_msg.get('content'), cwd)
        if not newest:
            raise ValueError('claude-code-bg: the newest user message has no text or image')
        first = not state.get('claude_id')
        block = messages.history_block(
            messages.unseen_history(history, None if first else state.get('seen')))
        prompt = f'{block}\n\n{newest}' if block else newest
        images = messages.saved_images(newest, cwd)
        if images:  # the retention cleanup deletes them with the conversation
            state['images'] = list(dict.fromkeys(state.get('images', []) + images))
        name = _session_name(conv)
        t0 = time.time()
        if first:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            persona = messages.persona()
            s = driver.dispatch(prompt, name=name, cwd=cwd, model=model, effort=effort,
                                persona=persona, hermes_session=hermes_session)
            state.update(claude_id=s.claude_id, session_id=s.session_id, cwd=cwd,
                         persona=bool(persona))
            op, offset, skip = 'dispatch', 0, 0
        else:
            old = driver.Session(state['claude_id'], state['session_id'])
            offset, skip = transcript.size(old.session_id), 0
            s = driver.follow_up(old, prompt, name=name, cwd=cwd, model=model, effort=effort)
            op = 'copy' if s.copied else 'follow-up'
            if s.session_id != old.session_id:
                # The copy's transcript repeats the history: skip the matches already there.
                offset, skip = 0, transcript.count_prompts(old.session_id, prompt)
            state.update(claude_id=s.claude_id, session_id=s.session_id)
        send_s = round(time.time() - t0, 2)
        store.event({'kind': 'send', 'conv': conv, 'turn': tkey, 'op': op,
                     'claude_id': s.claude_id, 'session_id': s.session_id, 'offset': offset,
                     'skip': skip, 'n_user': n_user, 'model': model, 'effort': effort,
                     'prompt': _cut(newest), 'history_chars': len(block), 'send_s': send_s})
        state['seen'] = messages.seen_marker(history)
        return {'state': 'sent', 'op': op, 'claude_id': s.claude_id, 'session_id': s.session_id,
                'offset': offset, 'skip': skip, 'prompt': prompt, 'n_user': n_user,
                'sent_at': t0, 'timing': {'send_s': send_s}}

    def _wait(self, request: _Request, conv: str, turn: dict, deadline: float) -> dict:
        """
        Poll the transcript every POLL_S until the turn is complete (the same verdict on two
        consecutive polls). About every 10 s read the agents row: `failed` or `blocked` on
        anything but a permission prompt raises; a permission prompt is denied with
        DENY_GUIDANCE and noted in the reply; a row that has finished while the last
        assistant entry stopped for another reason than `end_turn` raises (checked twice).
        Two cases end the wait although the completion rule does not hold: an end_turn with
        text that only in-flight background work holds back (unchanged for
        `inflight_grace_seconds()`) returns that text plus INFLIGHT_NOTE, and an end_turn
        without text on a finished row (seen on two row checks) returns EMPTY_REPLY.
        """
        cid = turn['claude_id']
        grace = inflight_grace_seconds()
        candidate: transcript.Turn | None = None
        held_since = 0.0  # when the current in-flight candidate was first seen
        suspect: str | None = None
        empty_checks = 0
        denied = False
        polls = 0
        while True:
            request.check()
            seen = self._inspect(turn)
            same = seen is not None and candidate is not None and candidate.sig == seen.sig \
                and candidate.blocker == seen.blocker
            if same and seen.blocker is None:
                return self._result(turn, seen.text, seen, denied)
            if seen is not None and seen.blocker == 'in_flight':
                if not same:
                    held_since = time.monotonic()
                elif time.monotonic() - held_since >= grace:
                    store.event({'kind': 'in_flight_grace', 'conv': conv, 'claude_id': cid,
                                 'grace_s': grace})
                    return self._result(turn, f'{seen.text}\n\n{INFLIGHT_NOTE}', seen, denied)
            if not same:
                empty_checks = 0
            candidate = seen
            if time.monotonic() > deadline:
                store.event({'kind': 'error', 'conv': conv, 'claude_id': cid, 'error': 'timeout'})
                raise TimeoutError(f'claude-code-bg: no reply from {cid} before the request '
                                   'timeout')
            if suspect or empty_checks or polls % ROW_CHECK_POLLS == 0:
                row = driver.agents().get(cid) or {}
                state, waiting = row.get('state'), row.get('waitingFor')
                if state == 'blocked' and waiting == 'permission prompt':
                    ok = driver.deny(cid, DENY_GUIDANCE)
                    store.event({'kind': 'deny', 'conv': conv, 'claude_id': cid, 'ok': ok})
                    if not ok:  # the driver stopped the turn instead
                        return self._result(turn, STOPPED_REPLY, None, True)
                    denied = True
                elif state == 'failed' or (state == 'blocked' and waiting):
                    raise self._error(conv, cid, f'session {cid} is {state}'
                                      + (f' ({waiting})' if waiting else ''))
                finished = _row_finished(row)
                if finished and seen is not None and seen.blocker == 'empty':
                    empty_checks += 1
                    if empty_checks >= 2:
                        store.event({'kind': 'empty_reply', 'conv': conv, 'claude_id': cid})
                        return self._result(turn, EMPTY_REPLY, seen, denied)
                else:
                    empty_checks = 0
                reason = (transcript.last_stop_reason(turn['session_id'], turn['offset'],
                                                      turn['prompt'], turn.get('skip', 0))
                          if finished else None)
                if reason and reason != 'end_turn':
                    if suspect == reason:
                        raise self._error(conv, cid, f'session {cid} ended the turn with '
                                          f'stop_reason {reason} and no reply')
                    suspect = reason
                else:
                    suspect = None
            polls += 1
            driver.nap(POLL_S)

    def _result(self, turn: dict, text: str, seen: transcript.Turn | None, denied: bool) -> dict:
        """The fields `_turn` merges into the turn record when the wait ends."""
        if denied and text != STOPPED_REPLY:
            text += f'\n\n{DENIED_NOTE}'
        return {'text': text,
                'usage': seen.usage if seen else copy.deepcopy(ZERO_USAGE),
                'n_end_turns': seen.n_end_turns if seen else 0, 'denied': denied,
                'timing': {**turn.get('timing', {}),
                           'total_s': round(time.time() - turn['sent_at'], 2)}}

    def _error(self, conv: str, cid: str, message: str) -> RuntimeError:
        store.event({'kind': 'error', 'conv': conv, 'claude_id': cid, 'error': message})
        return RuntimeError(f'claude-code-bg: {message}')

    def _response(self, model: str, tkey: str, turn: dict) -> Object:
        return self._reply(model, f'ccbg-{tkey}', turn.get('text') or '', turn['usage'])

    def _reply(self, model: str, response_id: str, text: str, usage: dict) -> Object:
        # Hermes rebuilds prompt tokens from these buckets and feeds its context compressor:
        # report the LAST API call's context, not the sum over a multi-call turn. The turn
        # sums stay in `turn_usage` (list-price estimates) and in events.jsonl.
        last = usage.get('last') or ZERO_USAGE['last']
        prompt_tokens = last['input'] + last['cache_read'] + last['cache_creation']
        output = usage.get('output', 0)
        return obj({
            'id': response_id, 'object': 'chat.completion', 'created': int(time.time()),
            'model': model,
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': text or '(empty reply)',
                                     'tool_calls': None}}],
            'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': output,
                      'total_tokens': prompt_tokens + output,
                      'prompt_tokens_details': {'cached_tokens': last['cache_read'],
                                                'cache_write_tokens': last['cache_creation']},
                      'cache_creation_input_tokens': last['cache_creation'],
                      'turn_usage': {k: usage.get(k, 0) for k in
                                     ('input', 'cache_read', 'cache_creation', 'output',
                                      'api_calls')}},
        })

    def _fault(self, where: str, conv: str, tkey: str) -> None:
        """
        Test hook for the live smoke suite: CLAUDE_CODE_BG_FAULT=<where> raises once per turn,
        after the turn record is saved, to show that a Hermes retry does not send twice.
        """
        if os.environ.get('CLAUDE_CODE_BG_FAULT') != where:
            return
        marker = store.base_dir() / f'fault-{conv}-{tkey}'
        if marker.exists():
            return
        marker.write_text(where)
        store.event({'kind': 'fault_injected', 'where': where, 'conv': conv, 'turn': tkey})
        raise RuntimeError(f'claude-code-bg injected fault ({where})')
