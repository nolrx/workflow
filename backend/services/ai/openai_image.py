"""
OpenAI (ChatGPT) image provider.

The official OpenAI image API *is* the OpenAI-compatible API, so this reuses the
exact openai-SDK image flow as the Panlaxy provider (``images.generate`` /
``images.edit`` + b64_json/url extraction) but points at OpenAI's own base URL
and reports ``provider_name == "openai"``. Verified against the live API:
``gpt-image-2`` returns ``b64_json`` for ``images.generate`` with size/quality.

Default model: ``gpt-image-2`` (also valid: gpt-image-1 / gpt-image-1.5 /
gpt-image-2-2026-04-21).
"""
from typing import Optional

from backend.services.ai.panlaxy import PanlaxyProvider

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2"


class OpenAIImageProvider(PanlaxyProvider):
    """Real OpenAI image API via the openai SDK.

    Same generate/edit flow as the OpenAI-compatible Panlaxy provider (so we don't
    duplicate the request/extraction logic), but defaults to OpenAI's own endpoint
    instead of Panlaxy's and identifies itself as ``openai``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_IMAGE_MODEL,
        base_url: Optional[str] = None,
        **kwargs,
    ):
        # base_url None -> OpenAI's own endpoint, never the inherited Panlaxy default.
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or DEFAULT_OPENAI_BASE_URL,
            **kwargs,
        )

    @property
    def provider_name(self) -> str:
        return "openai"
