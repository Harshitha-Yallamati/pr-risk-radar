import os
from openai import OpenAI

class LLMError(Exception):
    """Custom exception for LLM provider errors."""
    pass


PROVIDERS = [
    {
        "id": "latentstack",
        "env_var": "LATENTSTACK_API_KEY",
        "base_url": "https://latentstack.dev/v1",
        "default_model": "gemini/gemini-3.7-flash",
        "hint": "Get a key at https://latentstack.dev"
    },
    {
        "id": "gemini",
        "env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "hint": "Get a key at https://aistudio.google.com"
    },
    {
        "id": "groq",
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "hint": "Get a key at https://console.groq.com"
    },
    {
        "id": "cerebras",
        "env_var": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "llama3.1-70b",
        "hint": "Get a key at https://cloud.cerebras.ai"
    },
    {
        "id": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-2.5-flash",
        "hint": "Get a key at https://openrouter.ai"
    },
    {
        "id": "openai",
        "env_var": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "hint": "Get a key at https://platform.openai.com"
    }
]


def resolve_provider():
    selected_provider_id = os.getenv("LLM_PROVIDER")

    if selected_provider_id:
        provider = next((p for p in PROVIDERS if p["id"].lower() == selected_provider_id.lower()), None)
        if not provider:
            valid_providers = ", ".join(p["id"] for p in PROVIDERS)
            raise LLMError(f"Unknown LLM provider '{selected_provider_id}'. Valid options: {valid_providers}.")

        api_key = os.getenv(provider["env_var"])
        if not api_key:
            raise LLMError(
                f"Provider '{provider['id']}' was explicitly requested via LLM_PROVIDER, "
                f"but environment variable {provider['env_var']} is not set. ({provider['hint']})"
            )
        return provider, api_key

    # Auto-detect first provider with API key set
    for provider in PROVIDERS:
        api_key = os.getenv(provider["env_var"])
        if api_key:
            return provider, api_key

    env_vars = ", ".join(f"{p['id']}: {p['env_var']}" for p in PROVIDERS)
    raise LLMError(
        f"No LLM API key set in environment. Set LLM_PROVIDER or one of the following API keys: {env_vars}."
    )


def complete_prompt(prompt, max_tokens=4000):
    """
    Expose one function that takes a prompt and max_tokens and returns the response text.
    """
    provider, api_key = resolve_provider()
    model = os.getenv("LLM_MODEL") or provider["default_model"]

    client = OpenAI(
        api_key=api_key,
        base_url=provider["base_url"]
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        raise LLMError(f"LLM request to provider '{provider['id']}' failed: {str(e)}")
