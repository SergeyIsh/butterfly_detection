"""Small Tasks API transport and crash-safe, create-only provisioning journal."""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_BASE = 'https://tasks.yandex.ru/api/v1'
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class ProvisioningError(Exception):
    """A local validation or recovery requirement; no remote body is included."""


class ApiError(Exception):
    """definitive=True means a rejected request is safe to retry explicitly."""

    def __init__(self, message, *, status=None, code=None, request_id=None, definitive=False):
        self.status, self.code, self.request_id = status, code, request_id
        self.definitive = definitive
        details = [str(status)] if status is not None else []
        details += [str(value) for value in (code, request_id) if value]
        super().__init__(message + (' (' + ', '.join(details) + ')' if details else ''))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, token, auth_scheme='OAuth', timeout=30):
        if (
            not isinstance(token, str)
            or not token
            or any(char.isspace() or ord(char) < 32 for char in token)
        ):
            raise ProvisioningError('A nonempty token without whitespace is required')
        if auth_scheme not in ('OAuth', 'ApiKey'):
            raise ProvisioningError('Auth scheme must be OAuth or ApiKey')
        if (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ProvisioningError('Timeout must be a positive number')
        self._token, self._scheme, self._timeout = token, auth_scheme, timeout
        self._opener = build_opener(_NoRedirect())

    def _metadata(self, value):
        if not isinstance(value, str) or self._token in value:
            return None
        return value if re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else None

    def request(self, method, path, payload=None):
        if (
            method not in ('GET', 'POST')
            or not isinstance(path, str)
            or not re.fullmatch(r'/(projects|skills|pools)(/[A-Za-z0-9_-]{1,128})?', path)
        ):
            raise ProvisioningError('Only Tasks resource GET and collection POST are supported')
        if method == 'POST' and path.count('/') != 1:
            raise ProvisioningError('POST is only supported for resource creation')
        if method == 'GET' and payload is not None:
            raise ProvisioningError('GET requests must not contain a payload')
        url = API_BASE + path
        data = _canonical(payload).encode('utf-8') if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                'Authorization': self._scheme + ' ' + self._token,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ApiError('API response exceeds size limit')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ApiError('API response must be a JSON object')
            return result
        except HTTPError as error:
            try:
                body = json.loads(error.read(65536))
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, OSError):
                body = {}
            finally:
                error.close()
            raise ApiError(
                'Tasks API rejected the request',
                status=error.code,
                code=self._metadata(body.get('code')),
                request_id=self._metadata(body.get('request_id')),
                definitive=400 <= error.code < 500 and error.code != 408,
            ) from None
        except (URLError, OSError):
            raise ApiError(
                'Tasks API transport failed; request outcome may be unknown'
            ) from None
        except (ValueError, UnicodeError):
            raise ApiError(
                'Tasks API returned invalid JSON; request outcome may be unknown'
            ) from None


def _canonical(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
        )
    except (TypeError, ValueError):
        raise ProvisioningError('Plan must contain only finite JSON values') from None


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _resolve(value, resources):
    if isinstance(value, str) and value.startswith('$ref:'):
        role = value[5:]
        if role not in resources:
            raise ProvisioningError('Plan contains an unresolved resource reference')
        return resources[role]['id']
    if isinstance(value, list):
        return [_resolve(item, resources) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, resources) for key, item in value.items()}
    return value


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _load_state(path, steps, fingerprint):
    if not path.exists():
        if path.is_symlink():
            raise ProvisioningError('State path must not be a symbolic link')
        return {
            'version': 1,
            'fingerprint': fingerprint,
            'resources': {},
            'pending': None,
        }
    if path.is_symlink() or not path.is_file():
        raise ProvisioningError('State must be a regular file')
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise ProvisioningError('State contains invalid JSON') from None
    if not isinstance(state, dict) or set(state) != {
        'version', 'fingerprint', 'resources', 'pending'
    }:
        raise ProvisioningError('Invalid state schema')
    if (
        type(state['version']) is not int
        or state['version'] != 1
        or state['fingerprint'] != fingerprint
    ):
        raise ProvisioningError(
            'State version or plan fingerprint differs; use the original configuration'
        )
    resources = state['resources']
    if not isinstance(resources, dict) or len(resources) > len(steps):
        raise ProvisioningError('Invalid saved resources')
    if set(resources) != {step['role'] for step in steps[:len(resources)]}:
        raise ProvisioningError('Saved resources must form an ordered plan prefix')
    for step in steps[:len(resources)]:
        item = resources[step['role']]
        if (
            not isinstance(item, dict)
            or set(item) != {'id', 'endpoint'}
            or not _valid_id(item['id'])
            or item['endpoint'] != step['endpoint']
        ):
            raise ProvisioningError('Invalid saved resource identity')
    pending = state['pending']
    if pending is not None:
        if (
            not isinstance(pending, dict)
            or set(pending) != {'role', 'payload_sha256'}
            or len(resources) == len(steps)
        ):
            raise ProvisioningError('Invalid pending operation')
        step = steps[len(resources)]
        if (
            pending['role'] != step['role']
            or pending['payload_sha256'] != _digest(_resolve(step['payload'], resources))
        ):
            raise ProvisioningError('Pending operation does not match the resolved plan')
    return state


def _save_state(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(_canonical(state) + '\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_resource(resource, step, payload, resource_id):
    if not isinstance(resource, dict) or resource.get('id') != resource_id:
        raise ProvisioningError('Saved/adopted resource identity does not match')
    for key in ('project_id', 'name', 'private_name', 'public_name'):
        if key in payload and resource.get(key) != payload[key]:
            raise ProvisioningError('Saved/adopted resource identifying fields do not match')
    _check_pool(resource, step)


def _check_pool(resource, step):
    if step['endpoint'] == '/pools' and resource.get('status') != 'CLOSED':
        raise ProvisioningError(
            'Pool is not CLOSED; its ID is preserved, inspect it before continuing'
        )


def apply_plan(steps, state_path, client, adopt=None):
    """Create resources once; ambiguous POSTs require verified explicit adoption.

    adopt is an optional {pending_role: existing_id} mapping. No resource is
    opened, uploaded to, modified, deleted, or recreated by this function.
    """
    fingerprint = _digest(steps)
    path = Path(state_path).expanduser().absolute()
    if not path.parent.is_dir():
        raise ProvisioningError('State parent directory does not exist')
    try:
        descriptor = os.open(
            str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
    except OSError:
        raise ProvisioningError('Cannot open the state lock safely') from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProvisioningError('State lock must be a regular file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProvisioningError(
                'Another provisioning process holds the state lock'
            ) from None
        state = _load_state(path, steps, fingerprint)
        pending = state['pending']
        if adopt is not None and (
            not isinstance(adopt, dict)
            or pending is None
            or set(adopt) != {pending['role']}
            or not _valid_id(adopt[pending['role']])
        ):
            raise ProvisioningError(
                'Adoption must supply only the pending role and a valid resource ID'
            )
        if pending and not adopt:
            raise ProvisioningError(
                'Previous POST outcome is unknown; inspect Tasks and use --adopt ROLE=ID'
            )
        for step in steps:
            role, endpoint = step['role'], step['endpoint']
            payload = _resolve(step['payload'], state['resources'])
            saved = state['resources'].get(role)
            if saved:
                resource = client.request('GET', endpoint + '/' + saved['id'])
                _check_resource(resource, step, payload, saved['id'])
                continue
            if adopt and role in adopt:
                resource = client.request('GET', endpoint + '/' + adopt[role])
                _check_resource(resource, step, payload, adopt[role])
            else:
                state['pending'] = {'role': role, 'payload_sha256': _digest(payload)}
                _save_state(path, state)
                try:
                    resource = client.request('POST', endpoint, payload)
                except ApiError as error:
                    if error.definitive:
                        state['pending'] = None
                        _save_state(path, state)
                    raise
            if not isinstance(resource, dict) or not _valid_id(resource.get('id')):
                raise ProvisioningError(
                    'POST returned no valid ID; outcome is unknown, use --adopt ROLE=ID'
                )
            state['resources'][role] = {'id': resource['id'], 'endpoint': endpoint}
            state['pending'] = None
            _save_state(path, state)
            _check_pool(resource, step)
        return state
    finally:
        os.close(descriptor)
