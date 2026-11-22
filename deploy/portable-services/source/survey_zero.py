"""Forward an authenticated survey-zero action to the shared pose estimator."""
import asyncio
import json
from pathlib import Path
from uuid import UUID
from aiohttp import web


async def zero_survey(body):
    try:
        if set(body) != {'type', 'request_id', 'world_id'} or body['type'] != 'survey_zero':
            raise ValueError()
        UUID(body['request_id'])
        UUID(body['world_id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        return web.json_response({'error': {'message':'Invalid survey zero request'}}, status=400)
    async def exchange():
        state = Path.home()/'.local/share'
        binding = state/'biome-rover-server/survey-estimator.json'
        if binding.is_file():
            from biomerte.host_io_client import HostIOClient
            client = HostIOClient.from_config(binding)
            return await asyncio.to_thread(client.survey_estimator_zero, body['world_id'], body['request_id'])
        path = state/'biome-robot-vision/survey-pose.sock'
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
        if not isinstance(result, dict):
            raise ValueError('Invalid survey estimator response')
        if result.get('ok') is True and (result.get('reset_id'), result.get('world_id')) != (
                body['request_id'], body['world_id']):
            raise ValueError('Survey estimator acknowledgment does not match the request')
    except (OSError, ValueError, asyncio.TimeoutError):
        return web.json_response({'error': {'message':'Survey estimator did not acknowledge the reset'}}, status=503)
    if result.get('ok') is not True:
        message = result.get('error', 'Survey zero unavailable')
        if message == 'survey_request_rejected':
            message = 'Survey estimator rejected the reset. Verify the surveyed world and a fresh front IMU attitude.'
        return web.json_response({'error': {'message':message}}, status=409)
    return web.json_response(result)
