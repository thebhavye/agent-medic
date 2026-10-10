"""One chat-with-tools interface over several model providers.

    LLM_PROVIDER=anthropic   Claude via the Anthropic SDK (ANTHROPIC_API_KEY)
    LLM_PROVIDER=xai         Grok via xAI's OpenAI-compatible API (XAI_API_KEY)
    LLM_PROVIDER=ollama      a local model via Ollama's OpenAI-compatible API (free)
    LLM_PROVIDER=ollama_cloud  a model on Ollama's cloud (OLLAMA_API_KEY)
    LLM_PROVIDER=bedrock     Claude on Amazon Bedrock (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION)
    LLM_PROVIDER=scripted    a deterministic policy instead of a model (development and tests)

The agent keeps a provider-neutral transcript:
    {"role": "user", "content": str}
    {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "args"}], "raw": <provider content>}
    {"role": "tool", "id": str, "name": str, "content": str, "is_error": bool}
Each provider converts it to its own wire format. Anthropic replays "raw" unchanged so
thinking blocks survive across turns.
"""

import json
import os
from dataclasses import dataclass, field

import neatlogs

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "ollama": "qwen3:4b", "scripted": "scripted-policy",
                  "ollama_cloud": "gpt-oss:20b", "bedrock": "us.anthropic.claude-haiku-5-5"}
DEFAULT_FIXER_MODELS = {"ollama_cloud": "gpt-oss:120b", "bedrock": "us.anthropic.claude-opus-5-5"}

# USD per million tokens (input, output). Override with PRICE_IN / PRICE_OUT for other models.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class Turn:
    text: str
    tool_calls: list = field(default_factory=list)   # [{"id", "name", "args"}]
    stop: str = "end"                                # end | tool_use | max_tokens | refusal
    input_tokens: int = 0
    output_tokens: int = 0
    raw: object = None


def provider_name(role: str = "target") -> str:
    """role is "target" (the research agent) or "fixer". FIXER_PROVIDER overrides LLM_PROVIDER."""
    explicit = os.environ.get(f"{role.upper()}_PROVIDER") or os.environ.get("LLM_PROVIDER")
    if explicit:
        return explicit
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"


def model_for(role: str) -> str:
    env = os.environ.get(f"{role.upper()}_MODEL")
    if env:
        return env
    name = provider_name(role)
    if role == "fixer" and name in DEFAULT_FIXER_MODELS:
        return DEFAULT_FIXER_MODELS[name]
    if name not in DEFAULT_MODELS:
        raise RuntimeError(f"set {role.upper()}_MODEL to a model your {name} account can use")
    return DEFAULT_MODELS[name]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    model = model.split("anthropic.", 1)[-1]     # Bedrock ids: us.anthropic.claude-...
    if os.environ.get("PRICE_IN") or os.environ.get("PRICE_OUT"):
        prices = (float(os.environ.get("PRICE_IN", 0)), float(os.environ.get("PRICE_OUT", 0)))
    else:
        prices = PRICES.get(model, (0.0, 0.0))   # local and scripted models cost nothing
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


class AnthropicProvider:
    def __init__(self, bedrock: bool = False) -> None:
        import anthropic
        self.bedrock = bedrock
        # Bedrock uses the classic InvokeModel path, which hackathon IAM policies usually grant.
        client = anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1")) if bedrock             else anthropic.Anthropic()
        self.client = neatlogs.wrap(client)

    def chat(self, model: str, system: str, transcript: list, tools: list, effort: str = "low") -> Turn:
        messages = []
        for m in transcript:
            if m["role"] == "user":
                messages.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                messages.append({"role": "assistant", "content": m["raw"]})
            else:
                block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"],
                         "is_error": m["is_error"]}
                # Parallel tool results go back in one user message.
                if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
        response = self.client.messages.create(
            model=model, max_tokens=8000, system=system, tools=tools,
            output_config={"effort": effort}, messages=messages)
        calls = [{"id": b.id, "name": b.name, "args": b.input} for b in response.content if b.type == "tool_use"]
        stop = {"refusal": "refusal", "max_tokens": "max_tokens"}.get(
            response.stop_reason, "tool_use" if calls else "end")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return Turn(text, calls, stop, response.usage.input_tokens, response.usage.output_tokens, response.content)

    def complete_json(self, model: str, system: str, prompt: str, schema: dict) -> tuple[dict, int, int]:
        params = dict(model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": prompt}],
                      output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}})
        if self.bedrock:   # server-side fallbacks are not available on Bedrock
            response = self.client.messages.create(**params)
        else:
            response = self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks="default")
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"model stopped with {response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text), response.usage.input_tokens, response.usage.output_tokens


class OpenAICompatProvider:
    """xAI and Ollama both speak the OpenAI chat-completions format."""

    BASE_URLS = {"xai": "https://api.x.ai/v1", "ollama": "http://127.0.0.1:11434/v1",
                 "ollama_cloud": "https://ollama.com/v1"}
    KEYS = {"xai": "XAI_API_KEY", "ollama_cloud": "OLLAMA_API_KEY"}

    def __init__(self, name: str) -> None:
        from openai import OpenAI
        key = os.environ.get(self.KEYS[name], "") if name in self.KEYS else "ollama"
        if name in self.KEYS and not key:
            raise RuntimeError(f"set {self.KEYS[name]} to use {name}")
        self.key = key
        base = os.environ.get("OLLAMA_BASE_URL", self.BASE_URLS["ollama"]) if name == "ollama" else self.BASE_URLS[name]
        self.name, self.base = name, base
        self.client = neatlogs.wrap(OpenAI(api_key=key, base_url=base, timeout=600))

    @staticmethod
    def _tools(tools: list) -> list:
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in tools]

    def chat(self, model: str, system: str, transcript: list, tools: list, effort: str = "low") -> Turn:
        messages = [{"role": "system", "content": system}]
        for m in transcript:
            if m["role"] == "user":
                messages.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m["text"] or None}
                if m["tool_calls"]:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["tool_calls"]]
                messages.append(msg)
            else:
                content = f"ERROR: {m['content']}" if m["is_error"] else m["content"]
                messages.append({"role": "tool", "tool_call_id": m["id"], "content": content})
        response = self.client.chat.completions.create(model=model, messages=messages, tools=self._tools(tools))
        choice = response.choices[0]
        calls = []
        for c in choice.message.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": c.function.arguments}
            calls.append({"id": c.id, "name": c.function.name, "args": args})
        stop = "max_tokens" if choice.finish_reason == "length" else "tool_use" if calls else "end"
        usage = response.usage
        return Turn((choice.message.content or "").strip(), calls, stop,
                    usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0)

    def complete_json(self, model: str, system: str, prompt: str, schema: dict) -> tuple[dict, int, int]:
        if self.name in ("ollama", "ollama_cloud"):
            return self._ollama_json(model, system, prompt, schema)
        response = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema}})
        text = response.choices[0].message.content or ""
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        missing = [k for k in schema.get("required", []) if k not in data]
        if missing:
            raise ValueError(f"model output is missing {missing}")
        usage = response.usage
        return data, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0

    def _ollama_json(self, model: str, system: str, prompt: str, schema: dict) -> tuple[dict, int, int]:
        """Ollama's native API, with a context window big enough for the prompt (Ollama's default
        would silently cut it). Some cloud models ignore `format`, so the schema is also stated in
        the instructions, JSON is pulled out of whatever comes back, and one follow-up asks for
        bare JSON if the first reply had none."""
        import httpx
        instructions = (f"{system}\n\nRespond with only a JSON object that matches this JSON schema, "
                        f"with no Markdown and no other text:\n{json.dumps(schema)}")
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        headers = {"Authorization": f"Bearer {self.key}"} if self.name == "ollama_cloud" else {}
        tokens_in = tokens_out = 0
        for _ in range(2):
            body = {"model": model, "stream": False, "format": schema, "messages": messages,
                    "options": {"num_ctx": 16384, "temperature": 0.2}}
            if self.name == "ollama":
                body["think"] = False      # small local models think for minutes otherwise
            with neatlogs.trace(f"{model} (json)", kind="LLM", model=model):
                resp = httpx.post(self.base.removesuffix("/v1") + "/api/chat", json=body, headers=headers,
                                  timeout=1200)
            resp.raise_for_status()
            reply = resp.json()
            tokens_in += reply.get("prompt_eval_count", 0)
            tokens_out += reply.get("eval_count", 0)
            content = reply["message"].get("content") or ""
            data = _extract_json(content)
            if data is not None:
                missing = [k for k in schema.get("required", []) if k not in data]
                if not missing:
                    return data, tokens_in, tokens_out
                feedback = f"Your JSON is missing {missing}."
            else:
                feedback = "That was not a JSON object."
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"{feedback} Reply again with only the JSON object."}]
        raise ValueError("the model did not return JSON matching the schema")


def _extract_json(text: str) -> dict | None:
    """The first JSON object in text, including one inside a ```json fence."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


class ScriptedProvider:
    """A deterministic stand-in for a model, for development and tests: search the question,
    read the top two documents, answer with what they say. It drives the real tools, the real
    knowledge-base API and real neatlogs traces, so everything except model judgment is
    exercised. It also honors a "double-check" instruction in the system prompt by searching
    and reading everything twice, like an over-literal model would."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, model: str, system: str, transcript: list, tools: list, effort: str = "low") -> Turn:
        question = transcript[0]["content"]
        params = {t["name"]: next(iter(t["input_schema"]["properties"])) for t in tools}
        results = [m for m in transcript if m["role"] == "tool"]
        rounds = 2 if "double-check" in system else 1

        searches = [r for r in results if r["name"] == "search_docs" and not r["is_error"]]
        reads = [r for r in results if r["name"] == "read_doc" and not r["is_error"]]
        errors = [r for r in results if r["is_error"]]

        def call(name: str, value: str) -> Turn:
            self.calls += 1
            return Turn("", [{"id": f"call_{self.calls}", "name": name, "args": {params[name]: value}}],
                        "tool_use", input_tokens=sum(len(str(m)) for m in transcript) // 4, output_tokens=20)

        def answer(text: str) -> Turn:
            return Turn(text, [], "end", input_tokens=sum(len(str(m)) for m in transcript) // 4,
                        output_tokens=len(text) // 4)

        if len(errors) >= 3 or (errors and errors[-1]["content"].startswith(("KeyError", "TypeError"))
                                and len(errors) >= 2):
            return answer(f"I could not retrieve the documents needed to answer: {errors[-1]['content']}")
        if len(searches) < rounds:
            return call("search_docs", question if not searches else question + " source")
        try:
            hits = json.loads(searches[0]["content"])
            ids = [h["id"] for h in hits[:2]]
        except (ValueError, KeyError, TypeError):
            return answer("I could not understand the search results.")
        if not ids:
            return answer("The knowledge base has no documents about this.")
        if len(reads) < rounds * len(ids):
            return call("read_doc", ids[len(reads) % len(ids)])
        return answer(" ".join(r["content"] for r in reads[:len(ids)]))

    def complete_json(self, model: str, system: str, prompt: str, schema: dict):
        raise ValueError("the scripted provider cannot write fixes; use a real model for the fixer")


def get_provider(role: str = "target"):
    name = provider_name(role)
    if name == "scripted":
        return ScriptedProvider()
    if name in ("anthropic", "bedrock"):
        return AnthropicProvider(bedrock=name == "bedrock")
    if name in ("xai", "ollama", "ollama_cloud"):
        return OpenAICompatProvider(name)
    raise RuntimeError(f"unknown LLM_PROVIDER {name!r}")
