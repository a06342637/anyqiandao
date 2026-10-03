from datetime import datetime, timedelta
from decimal import Decimal

from app.checkin_state import BEIJING
from app.db import balance_view, checkin_earned


def counters():
    return {'signed': 0, 'already': 0, 'failed': 0, 'uncertain': 0, 'earned': Decimal(0)}


def amount(value):
    return round(float(value), 4)


def checkin_statistics(store, span):
    settings = store.settings()
    zone = BEIJING
    now = datetime.now(zone)
    start = (now - timedelta(days={'day': 1, 'week': 7, 'month': 30}[span] - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since, until = start.timestamp(), now.timestamp()
    with store.connection() as snapshot:
        snapshot.execute('BEGIN')
        rows = [dict(row) for row in snapshot.execute('SELECT account_id,code,quota_before,quota_after,used_before,used_after,created,balance_source,reward_amount FROM checkins WHERE created>=? AND created<=? ORDER BY created,id', (since, until))]
        accounts = [dict(row) for row in snapshot.execute('SELECT id,login_enc,quota,validity,checkin_status,last_checkin,note FROM accounts ORDER BY position,created')]
        latest = {row['account_id']: balance_view(row) for row in snapshot.execute('SELECT * FROM (SELECT account_id,code,quota_before,quota_after,used_before,used_after,created,balance_source,reward_amount,ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY created DESC,id DESC) AS ranking FROM checkins WHERE created<=?) WHERE ranking=1', (until,))}
    totals = counters()
    series = {}
    cursor = start
    while cursor <= now:
        series[cursor.strftime('%Y-%m-%d')] = {'day': cursor.strftime('%m-%d'), **counters()}
        cursor += timedelta(days=1)
    per_account = {}
    legacy_records = unmeasured = 0
    for row in rows:
        earned = checkin_earned(row)
        measured = earned is not None
        gain = earned or Decimal(0)
        field = {'signed': 'signed', 'already_signed': 'already', 'uncertain': 'uncertain'}.get(row['code'], 'failed')
        entry = per_account.setdefault(row['account_id'], counters())
        bucket = series[datetime.fromtimestamp(row['created'], zone).strftime('%Y-%m-%d')]
        for target in (totals, entry, bucket):
            target[field] += 1
            target['earned'] += gain
        legacy_records += row['balance_source'] == 'legacy'
        unmeasured += row['code'] == 'signed' and row['balance_source'] == 'live' and not measured
    table = []
    for account in accounts:
        entry = per_account.get(account['id'], counters())
        balance = latest.get(account['id'])
        table.append({'account_id': account['id'], 'username': store.vault.open(account['login_enc'], f'login:{account["id"]}')['username'],
                      'note': account['note'], 'quota': account['quota'], 'validity': account['validity'], **entry, 'earned': amount(entry['earned']),
                      'last': balance['created'] if balance else account['last_checkin'], 'last_code': balance['code'] if balance else account['checkin_status'], 'last_balance': balance})
    table.sort(key=lambda item: (-item['earned'], -(item['signed'] + item['already']), item['username']))
    for bucket in series.values():
        bucket['earned'] = amount(bucket['earned'])
    known_balances = [Decimal(str(account['quota'])) for account in accounts if account['quota'] is not None]
    return {'range': span, 'since': since, 'until': until, 'timezone': zone.key, 'attempts': len(rows), **totals, 'earned': amount(totals['earned']),
            'balance_total': amount(sum(known_balances)), 'balance_accounts': len(known_balances), 'accounts_total': len(accounts),
            'series': list(series.values()), 'accounts': table, 'stats_retention_days': settings.stats_retention_days,
            'legacy_records': legacy_records, 'unmeasured': unmeasured}
