import { ProductId, SubscriptionId, TenantId, UserId } from './ids';

export interface Entitlement {
  id: SubscriptionId;
  productId: ProductId;
  tenantId?: TenantId;
  userId?: UserId; // optional: user-scoped subscriptions
  startsAt?: string; // ISO 8601 date
  endsAt?: string; // ISO 8601 date
  isActive: boolean;
  // metadata should not contain tokens/secrets or raw payment data
  metadata?: Record<string, unknown>;
}
