"""Chunked file transport over JSON-only wx.cloud.callContainer.

Transfers are private, bounded, and idempotent. Conversion work remains in engines.py.
Like the job registry, this initial transport requires a single service instance.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

CHUNK_BYTES = 256 * 1024


class TransferStart(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    size: int = Field(gt=0, le=20 * 1024 * 1024, strict=True)


class TransferChunk(BaseModel):
    offset: int = Field(ge=0, strict=True)
    data: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


def install(app, runtime: dict) -> None:
    router = APIRouter()
    locks = runtime['transfer_locks']

    def transferred(identifier: str, token: str):
        return runtime['owned'](runtime['transfers'], identifier, runtime['owner'](token))

    @router.post('/api/transfers', status_code=201)
    async def begin(request: TransferStart, x_client_token: Annotated[str, Header()] = ''):
        digest = runtime['owner'](x_client_token)
        runtime['sweep_expired']()
        pending = runtime['transfers']
        all_sources = list(runtime['uploads'].values()) + list(pending.values())
        if sum(entry['owner'] == digest for entry in pending.values()) >= 4:
            raise HTTPException(429, '同时上传的文件过多，请稍后重试。')
        personal = sum(entry['size'] for entry in all_sources if entry['owner'] == digest)
        global_usage = sum(entry['size'] for entry in all_sources) + sum(
            sum(file['size'] for file in job.get('result', {}).get('files', []))
            for job in runtime['jobs'].values())
        if personal + request.size > 100 * 1024 * 1024 or global_usage + request.size > 1024 * 1024 * 1024:
            raise HTTPException(429, '暂存空间已满，请稍后再试。')
        name = re.split(r'[/\\]', request.name)[-1]
        name = re.sub(r'[\x00-\x1f\x7f]', '', name)
        extension = Path(name).suffix.lower().lstrip('.')
        if extension not in ('pdf', 'docx', 'jpg', 'jpeg', 'png'):
            raise HTTPException(415, '仅支持 PDF、DOCX、JPG 和 PNG。')
        identifier = uuid.uuid4().hex
        directory = runtime['DATA_ROOT'] / identifier
        directory.mkdir()
        (directory / 'source.part').touch()
        entry = {'kind': 'transfer', 'id': identifier, 'owner': digest, 'name': name, 'format': extension,
                 'size': request.size, 'received': 0, 'createdAt': time.time(), 'expiresAt': time.time() + runtime['TTL']}
        runtime['persist'](directory, entry)
        pending[identifier] = entry
        locks[identifier] = asyncio.Lock()
        return {'id': identifier, 'received': 0, 'chunkBytes': CHUNK_BYTES}

    @router.put('/api/transfers/{identifier}/chunks')
    async def append(identifier: str, request: TransferChunk, x_client_token: Annotated[str, Header()] = ''):
        entry = transferred(identifier, x_client_token)
        lock = locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            entry = transferred(identifier, x_client_token)
            try:
                data = base64.b64decode(request.data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(422, '文件块编码无效。') from exc
            if not data or len(data) > CHUNK_BYTES:
                raise HTTPException(413, '文件块不能超过 256 KB 或为空。')
            path = runtime['DATA_ROOT'] / identifier / 'source.part'
            received = path.stat().st_size
            end = request.offset + len(data)
            if end > entry['size']:
                raise HTTPException(413, '上传数据超出声明的文件大小。')
            if request.offset < received:
                if end > received:
                    raise HTTPException(409, '文件块重叠，请重新上传。')
                with path.open('rb') as stream:
                    stream.seek(request.offset)
                    if stream.read(len(data)) != data:
                        raise HTTPException(409, '重复文件块内容不一致，请重新上传。')
                return {'received': end}
            if request.offset != received:
                raise HTTPException(409, '文件块顺序不一致，请重新上传。')
            with path.open('ab') as stream:
                stream.write(data)
            entry.update(received=end, expiresAt=time.time() + runtime['TTL'])
            runtime['persist'](path.parent, entry)
            return {'received': end}

    @router.post('/api/transfers/{identifier}/complete')
    async def finish(identifier: str, x_client_token: Annotated[str, Header()] = ''):
        digest = runtime['owner'](x_client_token)
        if identifier in runtime['uploads']:
            return runtime['public_upload'](runtime['owned'](runtime['uploads'], identifier, digest))
        transferred(identifier, x_client_token)
        lock = locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            entry = transferred(identifier, x_client_token)
            directory = runtime['DATA_ROOT'] / identifier
            part = directory / 'source.part'
            if part.stat().st_size != entry['size']:
                raise HTTPException(409, '文件尚未上传完整。')
            entry['finalizing'] = True
            try:
                metadata = await asyncio.to_thread(runtime['inspect_file'], part, entry['format'])
                part.rename(directory / f"source.{entry['format']}")
                completed = {key: value for key, value in entry.items() if key not in ('received', 'finalizing')}
                completed.update(kind='upload', expiresAt=time.time() + runtime['TTL'], **metadata)
                runtime['persist'](directory, completed)
                runtime['uploads'][identifier] = completed
                runtime['transfers'].pop(identifier, None)
                return runtime['public_upload'](completed)
            except ValueError as exc:
                runtime['remove_directory'](identifier)
                runtime['transfers'].pop(identifier, None)
                raise HTTPException(422, str(exc)) from exc
            finally:
                entry.pop('finalizing', None)
                locks.pop(identifier, None)

    @router.delete('/api/transfers/{identifier}', status_code=204)
    async def abort(identifier: str, x_client_token: Annotated[str, Header()] = ''):
        entry = transferred(identifier, x_client_token)
        if entry.get('finalizing'):
            raise HTTPException(409, '文件正在校验，请稍后重试。')
        async with locks.setdefault(identifier, asyncio.Lock()):
            transferred(identifier, x_client_token)
            runtime['remove_directory'](identifier)
            runtime['transfers'].pop(identifier, None)
        locks.pop(identifier, None)

    def output_path(identifier, file_id, token):
        entry = runtime['owned'](runtime['jobs'], identifier, runtime['owner'](token))
        if entry['status'] != 'done':
            raise HTTPException(409, '结果尚未生成。')
        directory = runtime['DATA_ROOT'] / identifier
        if file_id is None:
            path = directory / 'preview.png'
        else:
            file = next((item for item in entry['result']['files'] if item['id'] == file_id), None)
            if not file:
                raise HTTPException(404, '结果文件不存在。')
            path = directory / file['disk']
        if not path.is_file():
            raise HTTPException(404, '结果文件已清理。')
        return path

    def encoded_chunk(path, offset, length):
        total = path.stat().st_size
        if offset >= total:
            raise HTTPException(416, '下载偏移超出文件范围。')
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
        return {'offset': offset, 'total': total, 'data': base64.b64encode(data).decode('ascii')}

    @router.get('/api/jobs/{identifier}/files/{file_id}/chunk')
    async def result_chunk(identifier: str, file_id: str, offset: Annotated[int, Query(ge=0)] = 0,
                           length: Annotated[int, Query(ge=1, le=CHUNK_BYTES)] = CHUNK_BYTES,
                           x_client_token: Annotated[str, Header()] = ''):
        path = output_path(identifier, file_id, x_client_token)
        return encoded_chunk(path, offset, length)

    @router.get('/api/jobs/{identifier}/preview/chunk')
    async def preview_chunk(identifier: str, offset: Annotated[int, Query(ge=0)] = 0,
                            length: Annotated[int, Query(ge=1, le=CHUNK_BYTES)] = CHUNK_BYTES,
                            x_client_token: Annotated[str, Header()] = ''):
        path = output_path(identifier, None, x_client_token)
        return encoded_chunk(path, offset, length)

    app.include_router(router)
