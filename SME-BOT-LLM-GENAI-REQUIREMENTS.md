mcropsey  [1:27 PM]
@SME Bot - API Sec what are you looking for to show GenAI and LLM in the discovery as those are separate tags?
SME Bot - API Sec  [1:28 PM]
TL;DR: GenAI and LLM are separate automatic Insight tags based on observed API URLs, parameters, headers, and request/response bodies; tagging may update as traffic and schemas evolve. API Technology Insight Tagging API Tags

LLM: Look for text-model traffic, such as requests with prompts/messages and… mcropsey  [1:32 PM]
@SME Bot - API Sec what is the difference between LLM and GenAI in the discovery?
SME Bot - API Sec  [1:32 PM]
TL;DR: LLM identifies text-generation or text-transformation APIs; GenAI identifies APIs generating non-text media such as images, audio, or video. API Technology Insight Tagging

Difference
* LLM: Traffic typically includes prompts/messages and model parameters, with completion/output or token-usage fields, plus known LLM vendor, endpoint, or header patterns. API Technology Insight Tagging
* GenAI: Traffic typically includes image, audio, or video generation endpoints and related vendor or model naming patterns. API Technology Insight Tagging

Both are automatically generated insight tags after API learning. They analyze the API URL, query parameters, and request/response body; tags may appear after additional traffic and change as the schema evolves. API Technology Insight Tagging API Tags

The Official Documentation Center does not publish the exact pattern list, scoring thresholds, or minimum traffic volume required for either tag.mcropsey  [1:56 PM]
@SME Bot - API Sec how are you classifying in the GeN AI view?  I see mcp as mcp, but I have gen ai and llm simulating on a server, it shows up in my API inventory but not as LLM GenAISME Bot - API Sec  [1:57 PM]
TL;DR: MCP has explicit protocol signals; LLM/GenAI tags require learned API traffic and matching classifier signals. An API appearing in Inventory does not guarantee either tag.

Classification
MCP is identified from JSON-RPC traffic that returns mcp-session-id or mcp-protocol-version and uses a recognized method such as initialize, tools/list, or tools/call. Processed / Ignored API Traffic

LLM and GenAI are separate automatic insight tags applied after API learning; they may appear only after additional traffic as the schema evolves. API Technology Insight Tagging API Tags

Signals used
- LLM: Text-generation or transformation traffic with prompt/message and model parameters in requests; completion/output or token-usage fields in responses; and recognized vendor, endpoint, or header patterns. API Technology Insight Tagging
- GenAI: Non-text generation—most commonly images, and sometimes audio or video—with media-generation endpoints and related vendor/model naming patterns. Text-only simulations are expected to align with LLM, not GenAI. API Technology Insight Tagging

Simulator checks
Send successful 2xx transactions with structured request/response bodies and valid mandatory fields. For LLM, include recognizable text-generation semantics in both directions; for GenAI, use media-oriented endpoints and model/vendor names. Processed / Ignored API Traffic API Technology Insight Tagging

Confirm the traffic source forwards the URL, relevant headers, query parameters, and full request/response bodies. Capture limits can reduce accuracy, and APIs exceeding source limits may not be fully discoverable. API Tags Guidelines for Selecting a Traffic Source Integration

The classifier evaluates schemas, successful samples, URLs, headers, and request/response bodies against configurable regex patterns, accumulates a score, and applies a tag only after the configured threshold is reached. It supports only one final classification per API; the documentation does not describe simultaneous LLM/GenAI/MCP classifications. LLM/GenAI Classifier (Confluence — may be outdated)[1:57 PM]Compare a simulator transaction that appears only in Inventory—including URL/path, headers, bodies, status, and source limits. If qualifying traffic still receives no tag, provide the sanitized request/response pair, API URL/ID, tenant version, and timestamp to Support for classifier-pattern/threshold validation. The Official Documentation Center does not publish the exact signatures, scoring thresholds, minimum sample count, or tenant-specific pattern configuration needed to guarantee either tag.