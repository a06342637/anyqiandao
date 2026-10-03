"""Helpers for backup round trips and compatibility with old encrypted files."""
import base64
import json

from app.backup_encryption import decrypt_bytes

PASSWORD = 'synthetic-backup-password-1234'
HEADERS = {'X-Backup-Password': base64.b64encode(PASSWORD.encode()).decode()}


async def export_document(client):
    response = await client.post('/api/v1/backup')
    assert response.status_code == 200, response.text
    return response.json()
