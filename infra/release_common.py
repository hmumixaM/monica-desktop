"""Shared release format and GitHub transport; compatible with NAS Python 3.8."""
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = 'hmumixaM/monica-next-crm'
WORKFLOW = '.github/workflows/ci.yml'
ROLES = ('api', 'worker', 'web')
AUDIT_PATHS = ('services/api/app/source_time.py', 'services/api/app/platform_store.py',
               'services/api/app/config.py', 'services/api/app/db.py',
               'services/api/app/message_key_audit.py', 'services/api/uv.lock', 'services/api/pyproject.toml')
INPUT_PATHS = {
    'api': ('services/api/app', 'services/api/Dockerfile', 'services/api/pyproject.toml',
            'services/api/uv.lock', 'migrations', 'infra/wechat/prefetch-sns-images.py', '.dockerignore', WORKFLOW),
    'worker': ('services/api/app', 'services/api/Dockerfile', 'services/api/pyproject.toml',
               'services/api/uv.lock', 'services/worker/worker.py', '.dockerignore', WORKFLOW),
    'web': ('apps/web', 'packages/contracts', 'package.json', 'package-lock.json',
            '.dockerignore', WORKFLOW),
}

HTTP_ONLY_PATHS = ('services/api/app/main.py', 'services/api/app/profile_summary.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(sha, paths, exclude=()):
    records = subprocess.check_output(['git', 'ls-tree', '-r', '-z', sha, '--', *paths]).split(b'\0')
    excluded = {p.encode() for p in exclude}
    return digest(b'\0'.join(row for row in records if row and row.split(b'\t', 1)[1] not in excluded) + b'\0')


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def valid_image(role, image):
    return isinstance(image, str) and re.fullmatch(
        r'ghcr\.io/hmumixam/monica-next-' + role + r'@sha256:[0-9a-f]{64}', image)


def validate_v2(manifest):
    if manifest.get('version') != 2 or manifest.get('repository') != REPOSITORY:
        raise ValueError('Expected this repository v2 release')
    if not re.fullmatch('[0-9a-f]{40}', manifest.get('commit', '')):
        raise ValueError('Invalid release SHA')
    if manifest.get('workflow') != WORKFLOW or not str(manifest.get('ci_run', '')).isdigit():
        raise ValueError('Invalid CI provenance')
    if set(manifest.get('images', {})) != set(ROLES):
        raise ValueError('Incomplete image set')
    for role in ROLES:
        if not valid_image(role, manifest['images'][role]):
            raise ValueError('Invalid fixed image digest: ' + role)
    if manifest.get('bundle', {}).get('name') != 'deployment.tar':
        raise ValueError('Missing deployment archive')
    for value in (manifest['bundle'].get('sha256'), manifest.get('compose_sha256')):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('Invalid archive/Compose checksum')
    for name in ('audit_inputs', 'build_inputs'):
        values = manifest.get(name, {})
        expected = set(AUDIT_PATHS) if name == 'audit_inputs' else set(ROLES)
        if set(values) != expected or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in values.values()):
            raise ValueError('Invalid ' + name + ' checksums')
    for key in ('migration_input',):
        if key in manifest and not re.fullmatch('[0-9a-f]{64}', str(manifest[key])):
            raise ValueError('Invalid ' + key)
    if 'background_inputs' in manifest and (set(manifest['background_inputs']) != {'api', 'worker'}
            or any(not re.fullmatch('[0-9a-f]{64}', str(v)) for v in manifest['background_inputs'].values())):
        raise ValueError('Invalid background_inputs')
    files = manifest.get('files', {})
    if 'infra/deploy-release.py' not in files or 'infra/docker-compose.yml' not in files:
        raise ValueError('Incomplete deployment file manifest')
    if not manifest.get('migrations') or set(manifest.get('audit_inputs', {})) != set(AUDIT_PATHS):
        raise ValueError('Missing migration or audit provenance')
    for name, value in files.items():
        parts = name.split('/')
        if parts[0] not in ('infra', 'migrations') or any(p in ('', '.', '..') for p in parts):
            raise ValueError('Unsafe deployment path')
        if any(p.startswith(('.', 'Caddyfile')) for p in parts):
            raise ValueError('Protected runtime path in release')
        if not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('Invalid deployment file hash')
    migrations = {Path(p).name: value for p, value in files.items() if p.startswith('migrations/') and p.endswith('.sql')}
    if migrations != manifest['migrations']:
        raise ValueError('Migration/file checksums differ')
    if files['infra/docker-compose.yml'] != manifest['compose_sha256']:
        raise ValueError('Compose/file checksums differ')
    return manifest


class TransportError(RuntimeError):
    pass


class ConfigurationError(RuntimeError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            redirect.remove_header('Authorization')
        return redirect


class GitHub:
    def __init__(self, token, repository=REPOSITORY):
        if repository != REPOSITORY:
            raise ConfigurationError('Unexpected repository')
        self.token = token
        self.base = 'https://api.github.com/repos/' + repository
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, data=None, binary=False):
        url = path if path.startswith('https://api.github.com/') else self.base + path
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'monica-nas-cd'}
        body = None if data is None else json.dumps(data).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=120 if binary else 30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 429 or error.code >= 500 or (error.code == 403 and error.headers.get('X-RateLimit-Remaining') == '0'):
                raise TransportError('GitHub temporarily unavailable: HTTP ' + str(error.code)) from None
            raise ConfigurationError('GitHub rejected request: HTTP ' + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise TransportError('GitHub transport unavailable: ' + type(error).__name__) from None
        return raw if binary else json.loads(raw)

    def pages(self, path):
        for page in range(1, 101):
            values = self.request(path + ('&' if '?' in path else '?') + 'per_page=100&page=' + str(page))
            yield from values
            if len(values) < 100:
                return
        raise ConfigurationError('GitHub pagination exceeded supported history')

    def ancestor(self, older, newer):
        if older == newer:
            return True
        result = self.request('/compare/' + older + '...' + newer)
        return result['status'] in ('ahead', 'identical')
