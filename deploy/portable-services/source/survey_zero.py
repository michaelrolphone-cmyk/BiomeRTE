"""Forward an authenticated survey-zero action to the shared pose estimator."""
import asyncio
import json
from pathlib import Path
from uuid import UUID
from aiohttp import web


async def zero_survey(body):
    try:
        if set(body) != {'type', 'request_id', 'world_id'}:
            raise ValueError()
        UUID(body['request_id'])
        UUID(body['world_id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        return web.json_response({'error': {'message':'Invalid survey zero request'}}, status=400)
    async def exchange():
        path = Path.home()/'.local/share/biome-robot-vision/survey-pose.sock'
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(json.dumps(body).encode()+b'\n')
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        result = await asyncio.wait_for(exchange(), timeout=5)
    except (OSError, ValueError, asyncio.TimeoutError):
        return web.json_response({'error': {'message':'Survey estimator did not acknowledge the reset'}}, status=503)
    if result.get('ok') is not True:
        return web.json_response({'error': {'message':result.get('error','Survey zero unavailable')}}, status=409)
    return web.json_response(result)
