import hmac
import hashlib
import time
import logging
import requests

from config import APP_KEY, APP_SECRET, API_URL
from utils.http import _http_session
from utils.log_context import traced_operation

logger = logging.getLogger(__name__)


def generate_api_signature(params, secret):
    param_string = ''.join([f"{k}{v}" for k, v in sorted(params.items())])
    return hmac.new(secret.encode('utf-8'), param_string.encode('utf-8'), hashlib.sha256).hexdigest().upper()


def prepare_api_params(method, extra_params):
    params = {
        'method': method,
        'app_key': APP_KEY,
        'sign_method': 'sha256',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'format': 'json',
        'v': '2.0',
    }
    params.update(extra_params)
    params['sign'] = generate_api_signature(params, APP_SECRET)
    return params


@traced_operation("aliexpress.api")
def send_api_request_with_retry(all_params, max_retries=2):
    for attempt in range(max_retries):
        try:
            response = _http_session.post(API_URL, data=all_params, timeout=8)
            if response.status_code != 200:
                logger.warning(
                    "AliExpress HTTP failure method=%s status=%s attempt=%s/%s",
                    all_params.get("method", "unknown"),
                    response.status_code,
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
            data = response.json()
            if 'error_response' in data:
                logger.warning(
                    "AliExpress API rejected request method=%s code=%s attempt=%s/%s",
                    all_params.get("method", "unknown"),
                    data["error_response"].get("code", "unknown"),
                    attempt + 1,
                    max_retries,
                )
                if data['error_response'].get('code') == 'ApiCallLimit':
                    ban_time = 5 if '5 seconds' in data['error_response'].get('msg', '') else 1
                    if attempt < max_retries - 1:
                        time.sleep(ban_time + 0.5)
                        continue
                return data
            return data
        except requests.exceptions.Timeout:
            logger.warning(
                "AliExpress timeout method=%s attempt=%s/%s",
                all_params.get("method", "unknown"),
                attempt + 1,
                max_retries,
                exc_info=True,
            )
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
        except Exception:
            logger.warning(
                "AliExpress request failed method=%s attempt=%s/%s",
                all_params.get("method", "unknown"),
                attempt + 1,
                max_retries,
                exc_info=True,
            )
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
    logger.error(
        "AliExpress retries exhausted method=%s attempts=%s",
        all_params.get("method", "unknown"),
        max_retries,
    )
    return {'error_response': {'code': 'MaxRetriesExceeded', 'msg': 'فشلت جميع المحاولات'}}
