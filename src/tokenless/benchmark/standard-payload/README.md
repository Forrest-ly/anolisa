<!-- Copyright 2026 Alibaba Cloud

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. -->

# Tokenless Standard Test Payloads

Stable, fully deterministic payloads for verifying tokenless compression
behavior and sanity-checking compression rates in your own environment. They
are the reference inputs behind the user-guide page
[Compression rates and applicable scenarios](../../../docs/user-guide/en/token-saving/tokenless/compression-scenarios.md)
([中文版](../../../docs/user-guide/zh/token-saving/tokenless/compression-scenarios.md)).

All content is synthetic; the payloads contain no real user data, hosts, or
credentials.

## Manifest

| File | Scenario | Exercises |
|---|---|---|
| `schema_tools.json` | Function-calling schema array with verbose descriptions, examples and titles | `compress-schema --batch` (schema compression, high-savings case) |
| `response_api_records.json` | Structured API/tool response: envelope plus 48 repetitive records, null/empty values, `debug`/`trace`/`logs` fields | `compress-response` (high-savings case, includes array-tail stash) and `compress-toon` |
| `response_code.json` | Code-search results whose content is source code | `compress-response` (medium/low-savings case) |
| `response_prose.json` | Document-search results whose content is natural-language prose | `compress-response` (low-savings boundary case) |

## Running the check

With `tokenless` on PATH:

```bash
./run-standard-check.sh
```

Or point the script at a specific binary:

```bash
TOKENLESS_BIN=/path/to/tokenless ./run-standard-check.sh
```

The script runs each payload through the CLI inside an isolated
`TOKENLESS_DATA_DIR` (your real statistics and stash databases are never
touched) and prints the `stats summary --json` result per case. Compare the
`chars_saved_percent` / `tokens_saved_percent` values against the reference
table in the user-guide page.

Manual single-payload runs work the same way:

```bash
tokenless compress-response -f response_api_records.json --session-id stdpay-api
tokenless stats summary --json
```

## Regenerating the payloads

`gen_standard_payload.py` is the single source of truth for the payload
content. It uses only the Python standard library, contains no randomness,
and writes byte-identical output on every run:

```bash
python3 gen_standard_payload.py
```

The generated JSON files are committed so that the check script does not
require Python.

## Stability policy

These payloads are a published reference: the user guide quotes measured
compression rates for exactly these bytes. Treat them like a fixture set —

- Do not edit the generated JSON files by hand; change the generator and
  regenerate instead.
- Any change to the generator must re-measure and update the reference table
  in the user-guide page (both locales) in the same change.
- Do not rename files without updating the user guide and the check script.
