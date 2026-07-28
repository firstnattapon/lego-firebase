# Vendored Webull SDK wheel

`webull_openapi_python_sdk-2.0.15-1lego-py3-none-any.whl` is rebuilt from
Webull's `2.0.15` PyPI wheel.

Why: the upstream wheel constrains Python 3.12 to `cryptography<43`, while the
audited fixed floor is `cryptography>=48.0.1`. The SDK's current request signer
uses Python's standard-library HMAC; all files under `webull/` remain
byte-for-byte unchanged. Only two `Requires-Dist: cryptography...` lines in
`METADATA` change, then wheel `RECORD` is regenerated.

Provenance and controls:

- original SHA-256:
  `c9c5f4ee3c62c7ecbe45e4b1b19ebd944a04620e0aebd35851d2f8c41acc9a14`
- patched SHA-256:
  `eef88481073ab4ff998b3446d685322b92bd2ef5a0e090d00f9e46bb814c15ad`
- license/notice from the Apache-2.0 upstream wheel remain inside the wheel
- reproduce with `python tools/rebuild_webull_wheel.py ORIGINAL.whl vendor/OUTPUT.whl`
- `test_vendored_webull_wheel.py` validates RECORD, constraints and aggregate
  runtime-code hash

Do not replace this wheel without repeating UAT contract tests and `pip-audit`.
