from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from app.backup_targets import BackupError
from app.remote_backup_schema import BrowseInput, RemoteBackupSettings


def remote_backup_router(service, auth):
    router = APIRouter(prefix='/api/v1/remote-backup', dependencies=[Depends(auth.require)])

    @router.get('')
    async def status():
        return service.public()

    @router.put('')
    async def save(payload: RemoteBackupSettings):
        try:
            return service.save(payload)
        except BackupError as error:
            raise HTTPException(409, str(error)) from None

    @router.post('/run', status_code=202)
    async def run():
        try:
            service.launch()
            return service.public()
        except BackupError as error:
            raise HTTPException(409, str(error)) from None

    @router.post('/{kind}/test')
    async def test(kind: Literal['oss', 'sftp']):
        try:
            return await service.inspect(kind)
        except BackupError as error:
            raise HTTPException(400, str(error)) from None

    @router.post('/{kind}/browse')
    async def browse(kind: Literal['oss', 'sftp'], payload: BrowseInput):
        try:
            return await service.inspect(kind, payload.path)
        except BackupError as error:
            raise HTTPException(400, str(error)) from None

    @router.delete('/sftp/host-key')
    async def reset_host_key():
        try:
            service.require_idle()
            service.store.set_meta('remote_backup_host_key', '')
            service.log('管理员已重置 SFTP 主机指纹；下次成功连接时重新记录', 'warning')
            return service.public()
        except BackupError as error:
            raise HTTPException(409, str(error)) from None

    return router
