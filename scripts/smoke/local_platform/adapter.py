"""Loopback gateway platform for the live smoke suite.

POST /msg  {"chat_id": "c1", "text": "...", "media": ["/abs/img.png"], "user": "smoke"}
    injects a MessageEvent through `BasePlatformAdapter.handle_message` (the entry the Telegram
    adapter uses) and returns at once.
Every `send()` / `edit_message()` is appended to $SMOKE_LOCAL_OUTBOX as one JSON line.
"""
import asyncio
import datetime
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

PLATFORM = 'smokelocal'


def _outbox(record: dict) -> None:
    path = os.environ['SMOKE_LOCAL_OUTBOX']
    with open(path, 'a') as fh:
        fh.write(json.dumps({'t': round(time.time(), 3), **record}) + '\n')


class SmokeLocalAdapter(BasePlatformAdapter):
    """
    Loopback adapter: an aiohttp server on 127.0.0.1:$SMOKE_LOCAL_PORT feeds messages in,
    replies go to the outbox file.
    """

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        self.port = int(os.environ['SMOKE_LOCAL_PORT'])
        self._runner = None

    @property
    def name(self) -> str:
        """
        Display name of the platform.
        """
        return 'SmokeLocal'

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """
        Start the loopback HTTP server and write a `connected` outbox record.
        """
        from aiohttp import web
        app = web.Application()
        app.router.add_post('/msg', self._handle_post)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, '127.0.0.1', self.port).start()
        self._mark_connected()
        _outbox({'event': 'connected', 'port': self.port})
        return True

    async def disconnect(self) -> None:
        """
        Stop the HTTP server.
        """
        self._mark_disconnected()
        if self._runner:
            await self._runner.cleanup()

    async def _handle_post(self, request):
        from aiohttp import web
        body = await request.json()
        chat_id = str(body.get('chat_id') or 'c1')
        user = str(body.get('user') or 'smoke')
        media = [str(p) for p in body.get('media') or []]
        source = self.build_source(chat_id=chat_id, chat_name=chat_id, chat_type='dm',
                                   user_id=user, user_name=user)
        mid = uuid.uuid4().hex[:8]
        event = MessageEvent(text=body.get('text') or '',
                             message_type=MessageType.PHOTO if media else MessageType.TEXT,
                             source=source, message_id=mid, timestamp=datetime.datetime.now(),
                             media_urls=media, media_types=['image/png' for _ in media])
        _outbox({'event': 'inbound', 'chat_id': chat_id, 'message_id': mid,
                 'text': body.get('text'), 'media': media})
        asyncio.ensure_future(self.handle_message(event))
        return web.json_response({'ok': True, 'message_id': mid})

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None):
        """
        Record one outgoing message in the outbox.
        """
        mid = uuid.uuid4().hex[:8]
        _outbox({'event': 'send', 'chat_id': chat_id, 'message_id': mid, 'reply_to': reply_to,
                 'content': content})
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *args, **kwargs):
        """
        Record one message edit in the outbox.
        """
        _outbox({'event': 'edit', 'chat_id': chat_id, 'message_id': message_id,
                 'content': content})
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """
        No typing indicator on this platform.
        """
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """
        Every chat is a direct message named after its id.
        """
        return {'name': chat_id, 'type': 'dm', 'chat_id': chat_id}


def _enabled() -> Optional[dict]:
    port = os.environ.get('SMOKE_LOCAL_PORT')
    return {'port': port} if port else None


def register(ctx):
    """
    Plugin entry point: register the `smokelocal` platform.
    """
    ctx.register_platform(
        name=PLATFORM, label='Smoke Local', adapter_factory=SmokeLocalAdapter,
        check_fn=lambda: True, validate_config=lambda _cfg: True,
        is_connected=lambda _cfg: bool(os.environ.get('SMOKE_LOCAL_PORT')),
        required_env=['SMOKE_LOCAL_PORT'], install_hint='stdlib + aiohttp (bundled with Hermes)',
        env_enablement_fn=_enabled, allow_all_env='SMOKE_LOCAL_ALLOW_ALL_USERS', emoji='🧪')
