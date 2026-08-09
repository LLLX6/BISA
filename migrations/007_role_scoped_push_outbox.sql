-- Role-scoped Web Push bindings. No historical notification is backfilled, so
-- applying this migration cannot unexpectedly notify existing accounts.
CREATE TABLE push_subscriptions(
 id TEXT PRIMARY KEY,
 endpoint_hash TEXT NOT NULL UNIQUE,
 endpoint TEXT NOT NULL,
 p256dh TEXT NOT NULL,
 auth TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);

CREATE TABLE push_subscription_bindings(
 id TEXT PRIMARY KEY,
 subscription_id TEXT NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
 account_id TEXT NOT NULL,
 role TEXT NOT NULL,
 audience_kind TEXT NOT NULL,
 audience_id TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 deactivated_reason TEXT NOT NULL DEFAULT '',
 last_success_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(subscription_id,account_id,role,audience_kind,audience_id)
);

CREATE TABLE push_delivery_outbox(
 id TEXT PRIMARY KEY,
 notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
 binding_id TEXT NOT NULL REFERENCES push_subscription_bindings(id) ON DELETE CASCADE,
 target_kind TEXT NOT NULL,
 target_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN('pending','processing','delivered','expired','dead','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 available_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 claim_token TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT '',
 delivered_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(notification_id,binding_id)
);

CREATE INDEX idx_push_binding_audience
ON push_subscription_bindings(audience_kind,audience_id,active);

CREATE INDEX idx_push_outbox_claim
ON push_delivery_outbox(status,available_at,lease_until,expires_at);

CREATE TRIGGER bisa_notification_push_outbox
AFTER INSERT ON notifications
BEGIN
  INSERT OR IGNORE INTO push_delivery_outbox(
    id,notification_id,binding_id,target_kind,target_id,status,attempts,
    available_at,expires_at,claim_token,lease_until,last_error,delivered_at,
    created_at,updated_at)
  SELECT 'pushout_' || lower(hex(randomblob(16))), NEW.id, binding.id,
    NEW.target_kind, NEW.target_id, 'pending', 0, NEW.created_at,
    CASE WHEN NEW.expires_at<>'' THEN NEW.expires_at
      ELSE strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 day') END,
    '', '', '', '', NEW.created_at, NEW.created_at
  FROM push_subscription_bindings binding
  WHERE binding.active=1 AND binding.audience_kind=NEW.target_kind
    AND (binding.audience_id=NEW.target_id OR
         (NEW.target_kind='admin' AND NEW.target_id='admin'));
END;

CREATE TRIGGER bisa_notification_push_cancel
AFTER UPDATE ON notifications
WHEN NEW.acted_at<>'' OR NEW.dismissed_at<>''
BEGIN
  UPDATE push_delivery_outbox SET status='cancelled',claim_token='',
    lease_until='',last_error='notification_resolved',updated_at=CURRENT_TIMESTAMP
  WHERE notification_id=NEW.id AND status IN('pending','processing');
END;
