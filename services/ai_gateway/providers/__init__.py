"""Provider adapter contracts and supported provider scaffolds."""

from .azure_openai import AzureOpenAIAdapter
from .base import ProviderAdapter, ProviderError, ProviderRequest, ProviderResponse, ProviderTimeout
from .openai import OpenAIAdapter

__all__ = ["AzureOpenAIAdapter", "OpenAIAdapter", "ProviderAdapter", "ProviderError", "ProviderRequest", "ProviderResponse", "ProviderTimeout"]
