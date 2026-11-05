"""Actual worker workload: original roster contract, real SDK, fake transport."""
import hashlib
import importlib.metadata
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROSTER_SOURCE = "__TERRACE_ROSTER_SOURCE__"
CONTRACT_SOURCE = "__TERRACE_CONTRACT_SOURCE__"
LOCK_SOURCE = "__TERRACE_LOCK_SOURCE__"
WHEEL_VERSIONS = "__TERRACE_WHEEL_VERSIONS__"


def main():
    assert sys.version_info[:2] == (3, 12)
    sdk = Path('/run/terrace-membership-python')
    assert os.statvfs(sdk).f_flag & os.ST_RDONLY
    for name, version in WHEEL_VERSIONS.items():
        distribution = importlib.metadata.distribution(name)
        assert distribution.version == version
        assert Path(distribution.locate_file('')).resolve().is_relative_to(sdk)
    import gspread
    import _cffi_backend
    from cryptography.hazmat.bindings import _rust
    assert all(Path(module.__file__).resolve().is_relative_to(sdk) for module in [gspread,_cffi_backend,_rust])
    # These exact existing tests use real gspread/requests/Google auth objects,
    # SyntheticSheets.send and socket denial; no provider credential exists.
    with tempfile.TemporaryDirectory(prefix='roster-contract-') as directory:
        root = Path(directory)
        source = root / 'windmill/f/Membership/record_completed_membership.py'
        source.parent.mkdir(parents=True)
        source.write_text(ROSTER_SOURCE)
        contract = root / 'tests/membership/test_roster.py'
        contract.parent.mkdir(parents=True)
        contract.write_text(CONTRACT_SOURCE)
        spec = importlib.util.spec_from_file_location('synthetic_roster_contract', contract)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        assert suite.countTestCases() == 11
        with io.StringIO() as output:
            result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
        assert result.testsRun == 11 and result.wasSuccessful()
    return {'status':'synthetic-roster-contract-ok','tests':11,
            'sourceDigest':hashlib.sha256(ROSTER_SOURCE.encode()).hexdigest(),
            'contractDigest':hashlib.sha256(CONTRACT_SOURCE.encode()).hexdigest(),
            'lockDigest':hashlib.sha256(LOCK_SOURCE.encode()).hexdigest()}
