import json, os, time
import urllib.request, urllib.error
from .diagnostics import diagnose_response

class ProviderError(RuntimeError):
    def __init__(self, message, api_calls=0):
        super().__init__(message)
        self.api_calls = api_calls

class ChatProvider:
    """One HTTP request, no silent retries; usage is taken only from provider."""
    def __init__(self, config, *, api_key=None):
        self.config = config
        # Optional per-request credential; existing callers still use the environment.
        self._api_key = api_key

    def clear_api_key(self):
        # Empty string prevents a released provider from falling back to a shared key.
        self._api_key = ''

    def complete(self, model, messages, budget):
        c = self.config
        key = self._api_key if self._api_key is not None else os.getenv(c["api_key_env"], "")
        if not key or key == "replace_me":
            raise ProviderError("请配置 API Key；没有发起 API 调用。")
        if model.startswith("YOUR_"):
            raise ProviderError("请先在 config.json 配置真实模型名称。")
        body = {"model": model, "messages": messages, c["output_parameter"]: budget}
        request = urllib.request.Request(c["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=c["timeout_seconds"]) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not echo provider bodies that could contain credentials or prompt data.
            raise ProviderError(f"API HTTP {exc.code}; 请核对模型、端点和 output_parameter。", api_calls=1) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("API 网络连接失败或超时；服务端是否计费未知。", api_calls=1) from None
        elapsed = time.perf_counter() - started
        try:
            choice = result["choices"][0]
            usage = result.get("usage") or {}
            message = choice["message"]
            diagnostic = diagnose_response(message, usage, budget, choice.get("finish_reason"))
            content = message.get("content")
            return {"answer": content if isinstance(content, str) else "", "finish_reason": choice.get("finish_reason"), "refusal": message.get("refusal"), "usage": usage, "api_latency_seconds": elapsed, "response_model": result.get("model"), "api_calls": 1,
                    "response_id": result.get("id"), "system_fingerprint": result.get("system_fingerprint"), **diagnostic}
        except (KeyError, IndexError, TypeError):
            raise ProviderError("API 返回不兼容的响应；本次请求已经发出。", api_calls=1) from None
