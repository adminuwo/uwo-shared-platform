"""Provider adapter contracts and supported provider scaffolds."""

from .azure_openai import AzureOpenAIAdapter
from .base import ProviderAdapter, ProviderError, ProviderRequest, ProviderResponse, ProviderTimeout
from .openai import OpenAIAdapter
from .responses import parse_responses_payload

__all__ = ["AzureOpenAIAdapter", "OpenAIAdapter", "ProviderAdapter", "ProviderError", "ProviderRequest", "ProviderResponse", "ProviderTimeout", "parse_responses_payload"]
