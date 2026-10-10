## neatlogs SDK example: google_genai_multiagent: provider_model_retired

Found by Agent Medic from neatlogs traces of workflow `blog-creation`, case `ext-1010-122156`.

### What failed

2/2 runs fail; first failing span LLM gemini-2.5-flash

```
404 NOT_FOUND. {'error': {'code': 404, 'message': 'This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use models/gemini-3.8-flash for the latest features and improvements. We recommend you to use the Interactions API (https://ai.google.dev/gemini-api/docs/get-started).', 'status': 'NOT_FOUND'}}
```

### Diagnosis

- provider: model_retired: the provider no longer serves gemini-2.5-flash (HTTP 404); no working replacement confirmed
- dependencies: clean: no deprecation warnings
- history: no_recent_code_change: the example's code last changed 110 days ago (a9646e4 'chore: use ingest.neatlogs.com endpoint'); it fails at that same commit today, so something outside the code changed

### Fix

The code specifies the model name "gemini-2.5-flash", which the provider has retired, causing 404 errors when the LLM is invoked.

`agents.py`
```diff
- client = genai.Client()
- MODEL = "gemini-2.5-flash"
+ client = genai.Client()
+ MODEL = "gemini-3.8-flash"
```

### Verification

verified: the diagnosed failure is gone in all 2 real runs; 1/2 succeed with OK traces; 1 run(s) hit a different failure, recorded as a follow-up
- run `The future of renewable energy`: exit 0, 136.0s, trace `81ec550a567536514964f1b9cb81d54a` OK
- run `How batteries store grid power`: exit 1, 4.7s, trace `8c59221ba4fb9e6b804e2a15c664e030` ERROR

### Other problems seen while verifying (not fixed here)

- exit 1, trace ERROR: LLM gemini-3.8-flash: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.
- exit 1, trace ERROR: LLM gemini-3.8-flash: Server disconnected without sending a response.
- runtime warning: `edic\.medic\external\neatlogs\examples\sdk_examples\google_genai_multiagent\agents.py:29: RuntimeWarning: This package (`duckduckgo_search`) has been renamed to `ddgs`! Use `pip install ddgs` instead.`
- runtime warning: `edic\.medic\external\neatlogs\examples\sdk_examples\google_genai_multiagent\agents.py:58: RuntimeWarning: This package (`duckduckgo_search`) has been renamed to `ddgs`! Use `pip install ddgs` instead.`

### Rejected attempts

- attempt 1 (Update model name to gemini-3.8-flash): verification blocked: the provider quota is exhausted (429 RESOURCE_EXHAUSTED on gemini-3.8-flash); the diagnosed 404 did not recur. Resume when quota resets.
- attempt 2 (Handle model quota errors gracefully while using gemini-3.8-flash): rejected in review after verification: the fix changes 79 lines but a update_model should need about 6; adds a broad except block that would hide failures; calls neatlogs.error, which does not exist. It made the example run, but by swallowi
