"""Synthetic password and helpers for testing encrypted backup round trips."""
import base64
import json

from app.backup_encryption import decrypt_bytes

PASSWORD = 'synthetic-backup-password-1234'
HEADERS = {'X-Backup-Password': base64.b64encode(PASSWORD.encode()).decode()}


async def export_document(client):
    response = await client.post('/api/v1/backup', json={'passphrase': PASSWORD})
    assert response.status_code == 200, response.text
    return json.loads(decrypt_bytes(response.content, PASSWORD))
