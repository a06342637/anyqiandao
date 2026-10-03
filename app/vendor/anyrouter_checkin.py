import json


def execute_check_in(client, account_name, provider_config, headers):
    print(f'[NETWORK] {account_name}: Executing check-in')
    checkin_headers = headers.copy()
    checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
    sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
    response = client.post(sign_in_url, headers=checkin_headers, timeout=30)
    print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')
    if response.status_code == 200:
        try:
            result = response.json()
            if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
                print(f'[SUCCESS] {account_name}: Check-in successful!')
                return True
            error_msg = result.get('msg', result.get('message', 'Unknown error'))
            already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
            if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
                print(f'[SUCCESS] {account_name}: Already checked in today')
                return True
            print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
            return False
        except json.JSONDecodeError:
            if 'success' in response.text.lower():
                print(f'[SUCCESS] {account_name}: Check-in successful!')
                return True
            print(f'[FAILED] {account_name}: Check-in failed - Invalid response format')
            return False
    print(f'[FAILED] {account_name}: Check-in failed - HTTP {response.status_code}')
    return False
