# Compression Rates and Applicable Scenarios

[中文版](../../../zh/token-saving/tokenless/compression-scenarios.md)

The compression rate Tokenless reports is a per-payload metric. This page explains the expected compression rate and the factors behind it for each strategy in different scenarios, and provides a set of standard test payloads so you can verify compression behavior in your own environment.

## How the compression rate is computed

- Compression rate = (before − after) ÷ before, with sizes measured in UTF-8 bytes.
- Token counts use the `ceil(bytes ÷ 4)` estimate; no model tokenizer is invoked.
- Operations with no savings are not recorded: when the estimated token count does not drop, the CLI emits the original text and produces no statistics record.
- The aggregated rate in `stats summary` and dashboards covers only payloads that passed through Tokenless; it is not the session-wide saving rate. See [Interpret the saving rate correctly](measuring-savings.md#interpret-the-saving-rate-correctly) for the conversion.

## Applicable scenarios and reference rates per strategy

| Strategy | Applicable scenarios | Reference rate | Main factors |
|----------|----------------------|----------------|--------------|
| Schema compression | Many Function Calling tool definitions, verbose tool/parameter descriptions, examples present | ~57% | Description length, examples/title presence, parameter count |
| Response compression | Structured tool/API JSON responses: repetitive record arrays, null/empty values, debug fields, long strings | ~26%–78% | Structural redundancy, array length, string length, truncation thresholds |
| TOON encoding | Tabular JSON with uniform fields and repetitive records | 15%–40% | Record homogeneity, field count |
| Command rewriting (RTK) | Noisy build/test/package-manager command output | 60%–90% | Command type, share of noise in the output |

Reference rates are typical values observed on common workloads, not commitments; actual rates are determined by the content itself. Use the standard test payloads below to verify locally.

### Scenarios with high compression rates

- **Repetitive structured responses**: list endpoints, search results, bulk status queries. The longer the array and the more uniform the records, the higher the rate; array tails beyond the truncation threshold go into the Stash and stay retrievable, so compression remains end-to-end lossless.
- **API responses with redundant fields**: responses carrying `debug`, `trace`, or `logs` fields (default blacklist), `null` values, or empty strings/arrays/objects — that content is removed outright.
- **Sessions with many tool definitions**: when an agent registers dozens or hundreds of tools, schema compression strips examples, titles, and overly long descriptions from the definitions.
- **Command-line output**: noisy output from build tools, test frameworks, and package managers is filtered by RTK. RTK is a separate binary and works on command output rather than JSON payloads.

### Scenarios with low or no compression

| Scenario | Why | Expected behavior |
|----------|-----|-------------------|
| Short, compact responses | Compression yields no token savings | Original emitted, no statistics recorded (expected) |
| Natural-language prose (document retrieval, web pages) | Little removable structural redundancy | Low single digits to about ten percent |
| Source-code-dominated responses | Code itself has low redundancy | Around ten to twenty percent, depending on structure |
| High-entropy content: base64/binary, compressed or encrypted data, random strings | No redundancy to remove | Almost no savings |
| Output already trimmed upstream (fields filtered, pages truncated) | Redundancy already removed | Savings depend on the remaining content |
| Model reasoning output, system prompts, conversation history | Outside what Tokenless touches | Not involved |

Different adapters use different truncation thresholds (shared shell policy `65536`/`128`/`8`, other structured-tool policy `1048576`/`65536`/`32`; see [Adapter processing rules](framework-integration.md#adapter-processing-rules)), so the same content can measure differently through the standalone CLI than inside an agent.

## Verifying with the standard test payloads

The repository ships a set of deterministic standard payloads under [`src/tokenless/benchmark/standard-payload/`](https://github.com/alibaba/anolisa/tree/main/src/tokenless/benchmark/standard-payload), covering the typical scenarios from high to low compression:

| Payload | Scenario | Matching command |
|---------|----------|------------------|
| `schema_tools.json` | Function Calling schema array with verbose descriptions | `tokenless compress-schema --batch` |
| `response_api_records.json` | Structured API response (48 repetitive records, with debug/trace/logs fields) | `tokenless compress-response`, `tokenless compress-toon` |
| `response_code.json` | Code-search results (content is source code) | `tokenless compress-response` |
| `response_prose.json` | Document-search results (content is natural-language prose) | `tokenless compress-response` |

All payload content is synthetic and contains no real user data.

### Running

Clone the repository and run the bundled check script (requires an installed tokenless):

```bash
git clone https://github.com/alibaba/anolisa.git
cd anolisa/src/tokenless/benchmark/standard-payload
./run-standard-check.sh
```

Or download a single payload and run it by hand:

```bash
curl -fsSL -O https://raw.githubusercontent.com/alibaba/anolisa/main/src/tokenless/benchmark/standard-payload/response_api_records.json
tokenless compress-response -f response_api_records.json \
  --session-id stdpay-api
tokenless stats summary --json
```

### Reference results

The numbers below were measured with tokenless 0.7.6 and the default truncation thresholds on Linux x86_64. Character and token metrics are content-based and platform-independent, so they should reproduce on other supported platforms.

| Case | Input (bytes) | Output (bytes) | Chars saved | Est. tokens saved |
|------|---------------|----------------|-------------|-------------------|
| Schema compression (`schema_tools.json`) | 10,060 | 4,976 | ~50.5% | ~50.7% |
| Response compression · structured (`response_api_records.json`) | 37,018 | 15,579 | ~57.9% | ~57.9% |
| Response compression · code (`response_code.json`) | 5,991 | 4,927 | ~17.8% | ~17.8% |
| Response compression · prose (`response_prose.json`) | 4,697 | 4,410 | ~6.1% | ~6.1% |
| TOON encoding (`response_api_records.json`) | 37,018 | 29,475 | ~20.4% | ~20.4% |

### How to read the results

- **Standard-payload results differ markedly from the reference table**: first check `tokenless --version`, then confirm the input files match the repository (`gen_standard_payload.py` regenerates them and the output must be byte-identical to the committed files).
- **Your real workload compresses differently from the reference table**: that is expected — the rate is determined by content redundancy. Use the two scenario tables above to place your workload in the right band.
- **Estimating session-wide savings**: overall estimated saving ≈ payload compression rate × tool-response share of total session tokens; see [Interpret the saving rate correctly](measuring-savings.md#interpret-the-saving-rate-correctly).
