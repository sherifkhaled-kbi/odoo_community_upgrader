"""Read-only PostgreSQL dump inspection and conservative restore admission.

Custom-header layout follows PostgreSQL pg_backup_archiver.c ReadHead/ReadInt.
This is a bounded metadata parser, not an archive validator or SQL rewriter.
"""
import hashlib
import io
from pathlib import Path
import re

from .jobs import RecoveryError

SUPPORTED_MAJORS = range(13, 18)
HEADER_LIMIT = 256 * 1024


class RestoreFailure(RecoveryError):
    def __init__(self, reason, message, details=None):
        super().__init__('DATABASE_RESTORE_FAILURE: ' + message)
        self.reason = reason
        self.details = details or {}


def fail(reason, message):
    raise RestoreFailure(reason, message)


def version_number(text):
    if not isinstance(text, str):
        return None
    match = re.match(r'^(\d+(?:\.\d+)*)(?=\s|$)', text.strip())
    return match[1] if match else None


def client_version(text):
    match = re.search(r'\bPostgreSQL\)?\s+(\d+(?:\.\d+)*)(?=\s|$)', text)
    if not match:
        fail('RUNTIME_VERSION_UNKNOWN', 'could not identify the PostgreSQL client version')
    return match[1]


def _custom_header(prefix):
    stream = io.BytesIO(prefix)
    def read(length):
        result = stream.read(length)
        if len(result) != length:
            fail('INVALID_DUMP_HEADER', 'truncated custom archive header; obtain an intact backup')
        return result
    read(5)
    archive = tuple(read(3))
    if not (1, 10, 0) <= archive <= (1, 16, 255):
        fail('UNSUPPORTED_ARCHIVE_VERSION', 'unsupported custom archive header version; use a validated compatible PostgreSQL runtime')
    int_size, offset_size, archive_format = read(3)
    if int_size not in (1, 2, 4, 8) or not 1 <= offset_size <= 32 or archive_format != 1:
        fail('INVALID_DUMP_HEADER', 'invalid or non-custom PostgreSQL archive header')
    def integer():
        sign = read(1)[0]
        if sign not in (0, 1):
            fail('INVALID_DUMP_HEADER', 'invalid integer encoding in archive header')
        value = int.from_bytes(read(int_size), 'little')
        return -value if sign else value
    def string():
        length = integer()
        if length == -1:
            return None
        if not 0 <= length <= 4096:
            fail('INVALID_DUMP_HEADER', 'invalid bounded string in archive header')
        return read(length).decode('utf-8', errors='replace')
    if archive >= (1, 15, 0):
        if read(1)[0] not in (0, 1, 2, 3):
            fail('UNSUPPORTED_COMPRESSION', 'custom archive compression is not recognized')
    else:
        integer()  # Legacy compression level.
    for _ in range(7):
        integer()  # Creation timestamp; not evidence of server/client version.
    string()  # Database name deliberately discarded: no business identifiers in metadata.
    return {'format': 'custom', 'archive_version': '.'.join(map(str, archive)),
            'source_server_version': version_number(string()),
            'dump_client_version': version_number(string()),
            'transaction_timeout': False}


def inspect_dump(path):
    """Inspect bytes, not filename suffix or backup manifest's server-only hint."""
    with Path(path).open('rb') as stream:
        prefix = stream.read(HEADER_LIMIT)
    if prefix.startswith(b'PGDMP'):
        try:
            result = _custom_header(prefix)
        except RestoreFailure as exc:
            exc.details['dump'] = {'format': 'custom', 'archive_version': '.'.join(map(str, prefix[5:8])) if len(prefix) >= 8 else None}
            raise
    else:
        text = prefix.decode('utf-8', errors='replace')
        if not re.search(r'^-- PostgreSQL database dump\s*$', text, re.M):
            fail('UNSUPPORTED_DUMP_FORMAT', 'expected pg_dump plain SQL or PGDMP custom archive; ZIP inputs must contain dump.sql')
        def header(label):
            match = re.search(r'^-- ' + label + r' ([^\r\n]+)', text, re.M)
            return version_number(match[1]) if match else None
        timeout = re.search(r'^SET\s+transaction_timeout\s*=\s*\d+\s*;', text, re.M | re.I)
        result = {'format': 'plain_sql', 'archive_version': None,
                  'source_server_version': header('Dumped from database version'),
                  'dump_client_version': header('Dumped by pg_dump version'),
                  'transaction_timeout': bool(timeout)}
        if timeout:
            result['required_statement'] = timeout[0].strip()
    result['header_sha256'] = hashlib.sha256(prefix).hexdigest()
    return result


def required_major(metadata):
    source, exporter = metadata.get('source_server_version'), metadata.get('dump_client_version')
    if source is None or exporter is None:
        fail('DUMP_VERSION_UNKNOWN', 'dump server/pg_dump versions are missing or unrecognized; obtain a backup with intact pg_dump headers')
    return max(13, int(source.split('.')[0]), int(exporter.split('.')[0]),
               17 if metadata.get('transaction_timeout') else 13)


def select_runtime(requested, metadata):
    required = required_major(metadata)
    if required not in SUPPORTED_MAJORS:
        fail('UNSUPPORTED_POSTGRES_VERSION', f'dump requires PostgreSQL {required}+; this restore adapter supports 13–17. Retain the original and qualify a newer runtime')
    if requested == 'auto':
        return str(required)
    if not re.fullmatch(r'\d+(?:\.\d+)*', requested):
        fail('INVALID_POSTGRES_VERSION', 'use --postgres-version auto or a supported numeric version')
    major = int(requested.split('.')[0])
    if major not in SUPPORTED_MAJORS:
        fail('UNSUPPORTED_POSTGRES_VERSION', 'supported restore runtimes are PostgreSQL 13–17')
    if major < required:
        fail('POSTGRES_VERSION_MISMATCH',
             f'dump requires PostgreSQL {required}+ (server {metadata["source_server_version"]}, pg_dump {metadata["dump_client_version"]}); requested {requested}. Start a new job with --postgres-version auto or {required}; do not edit the dump')
    return requested


def validate_runtime(metadata, runtime, selected):
    required = required_major(metadata)
    server = int(runtime['server_version_num']) // 10000
    psql = int(runtime['psql_version'].split('.')[0])
    restore = int(runtime['pg_restore_version'].split('.')[0])
    expected = int(selected.split('.')[0])
    if server != expected or psql != server or restore != server:
        fail('RESTORE_RUNTIME_MISMATCH', 'actual PostgreSQL server/clients do not match the selected image major; inspect recorded runtime evidence')
    if server not in SUPPORTED_MAJORS or server < required:
        fail('POSTGRES_VERSION_MISMATCH', f'actual PostgreSQL {server} cannot safely load this dump; use PostgreSQL {required}+ within supported runtimes')


def sql_failure_evidence(path, stderr):
    """Only noncustomer SET statements are portable; raw SQL stays private."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors='replace')
    stderr = stderr or ''
    match = re.search(r'/tmp/input\.dump:(\d+):', stderr)
    line_number = int(match[1]) if match else None
    statement = None
    # pg_restore --verbose may report a generated SET not present as plain SQL.
    timeout = re.search(r'(?im)^\s*(SET\s+transaction_timeout\s*=\s*\d+\s*;)', stderr)
    if timeout:
        statement = timeout[1].strip()
    if statement is None and line_number and line_number <= 10000:
        with Path(path).open('rb') as stream:
            for number in range(1, line_number + 1):
                raw = stream.readline(16384)
                if not raw:
                    break
                if len(raw) == 16384 and not raw.endswith(b'\n'):
                    break  # No unbounded row/SQL reads into exported evidence.
                if number == line_number:
                    line = raw.decode(errors='replace').strip()
                    if re.fullmatch(r'SET\s+(?:transaction_timeout|statement_timeout|lock_timeout|idle_in_transaction_session_timeout)\s*=\s*\d+\s*;', line, re.I):
                        statement = line
    sqlstate = re.search(r'(?:ERROR|FATAL):\s+([0-9A-Z]{5}):', stderr)
    if re.search(r'unrecognized configuration parameter ["\']transaction_timeout["\']', stderr):
        error = 'unrecognized configuration parameter "transaction_timeout"'
    else:
        error = 'Restore command failed; raw SQL/error payload retained only in protected restore.private.log'
    return {'line': line_number, 'failing_statement': statement or '[withheld: inspect private restore log]',
            'relevant_error': error, 'sqlstate': sqlstate[1] if sqlstate else None}
