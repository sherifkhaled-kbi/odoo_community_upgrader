from pathlib import Path
import pytest


def pytest_addoption(parser):
    parser.addoption('--run-docker-restore-tests', action='store_true', default=False,
                     help='Create/restore/stop only a new labeled PostgreSQL 17 test container; retain its data, no DROP/prune')
    parser.addoption('--run-native-postgres-tests', action='store_true', default=False,
                     help='Run safe synthetic tests in a private temporary native PostgreSQL cluster (no TCP or DROP operations)')
    parser.addoption('--approve-disposable-docker-tests', action='store_true', default=False,
                     help='Explicitly authorize creation/initialization/stopping of isolated migration test resources')
    parser.addoption('--odoo16-test-backup', type=Path, help='Synthetic disposable Odoo 16 ZIP; never a production backup')
    parser.addoption('--test-postgres-version', default='13')


@pytest.fixture
def docker_test_settings(request):
    if not request.config.getoption('--approve-disposable-docker-tests'):
        pytest.skip('NOT TESTED: actual Docker/DB operations require explicit disposable-test approval')
    backup = request.config.getoption('--odoo16-test-backup')
    if backup is None or not backup.is_file():
        pytest.fail('An existing synthetic --odoo16-test-backup ZIP is required')
    return backup.resolve(), request.config.getoption('--test-postgres-version')
