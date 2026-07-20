"""Canonical identifiers shared by every UWO platform consumer."""

from enum import Enum


class Product(str, Enum):
    AISA = "aisa"
    AI_MALL = "ai-mall"
    AISA_CONNECT = "aisa-connect"
    AI_LEGAL_PROFESSIONAL = "ai-legal-professional"
    AI_ADS = "ai-ads"
    AI_CASHFLOW = "ai-cashflow"


class Capability(str, Enum):
    IDENTITY = "identity"
    TENANCY = "organisation-and-tenant"
    ENTITLEMENTS = "roles-and-entitlements"
    DASHBOARD = "dashboard-shell"
    BILLING = "billing-and-credits"
    AI_GATEWAY = "ai-gateway-and-model-router"
    STORAGE = "storage"
    NOTIFICATIONS = "notifications"
    ANALYTICS = "analytics"
    AUDIT = "audit"
    SECURITY = "security"
    CONNECTORS = "connectors"
    KNOWLEDGE = "knowledge-layer"


PRODUCTS = tuple(Product)
CAPABILITIES = tuple(Capability)
