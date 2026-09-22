# Cross-interpreter coverage

`cross_interpreter.py` is a smoke collector for two available Python
interpreters. It has Bash directly execute each
interpreter against `lib/model_requests.py`; the command itself is unchanged.
It fails unless each child writes raw data that includes that first-party
script, and it writes `coverage-cross-python.xml` outside the checkout.

Run it with:

```sh
rtk proxy python3 tests/coverage/cross_interpreter.py --output /tmp/ffs-coverage-smoke \
  --python python3 --python /usr/bin/python3
```

For the complete Python and Bats suite, use the collector wrapper. It runs
`python3 -m pytest lib/ tests/` and every first-party `*.bats` suite,
including the two under `scripts/`, even if the Python run fails. It makes an output-local copy of a coverage package only
after proving that the selected Python can import that copy. The package source
is discovered from `COVERAGE_SOURCE_PYTHON`, the selected Python, the runner,
and `python3`; it is also validated for the runner that combines the data. No
OS-specific interpreter path or global installation is used.
It then sets `COVERAGE_PROCESS_START` and adds its private coverage package and startup shim to
`PYTHONPATH`.

The full suite requires the controller-registered upstream-runtime descriptor
and its recorded SHA-256. Supply the sealed external pair; the collector
checks its exact bytes before it creates an output directory and does not
derive replacement runtime metadata from the host environment.

```sh
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR=/absolute/path/to/upstream-runtime.json \
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256=<recorded-lowercase-sha256> \
rtk proxy python3 tests/coverage/run-full-suite.py --output /tmp/ffs-coverage
```

For example, where `/usr/bin/python3` intentionally has no global Coverage.py,
borrow only a compatible package source from an existing virtual environment:

```sh
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR=/absolute/path/to/upstream-runtime.json \
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256=<recorded-lowercase-sha256> \
COVERAGE_SOURCE_PYTHON=/opt/project-venv/bin/python \
/usr/bin/rtk proxy python3 tests/coverage/run-full-suite.py --output /tmp/ffs-coverage
```

The wrapper combines raw files exactly once into `combined` and writes
`coverage.xml` when data exists. Its JSON record has `both_attempted` and
`full_suite_coverage`; `full_suite_coverage` is true only when Python, Bats,
and XML generation all succeed. An XML produced after a failed suite is
diagnostic evidence, not a coverage-gate result. Coverage's process-startup
guidance documents the `COVERAGE_PROCESS_START` plus `sitecustomize.py`
mechanism: <https://coverage.readthedocs.io/en/7.13.4/subprocess.html>.

For a suite that invokes another Python, declare it with `--child-python`.
For example, macOS can retain Python3.9-compatible private coverage for both
the system and Homebrew interpreters:

```sh
rtk proxy env \
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR=/absolute/path/to/upstream-runtime.json \
FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR_SHA256=<recorded-lowercase-sha256> \
COVERAGE_SOURCE_PYTHON=/usr/bin/python3 python3 \
  tests/coverage/run-full-suite.py --child-python /usr/bin/python3 \
  --output /private/tmp/ffs-coverage-mac
```
