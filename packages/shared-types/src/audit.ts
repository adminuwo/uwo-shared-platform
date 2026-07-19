import { AuditEventId, TraceId, UserId } from './ids';

export type AuditActor = {
  id?: UserId;
  displayName?: string;
};

export interface AuditEvent {
  id: AuditEventId;
  type: string; // canonical event type, e.g. 'user.created', 'subscription.updated'
  timestamp: string; // ISO 8601
  actor?: AuditActor;
  // target references (IDs) of resources impacted by the event
  targetIds?: string[];
  // trace id for correlating across services
  traceId?: TraceId;
  // optional non-sensitive details. Do NOT include passwords, tokens, secrets,
  // or full/raw sensitive payloads here.
  details?: Record<string, unknown>;
}
