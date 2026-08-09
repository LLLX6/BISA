-- Additive security and operational controls for BISA.
-- This migration does not remove or rename an existing table or column.

ALTER TABLE merchants ADD COLUMN active INTEGER NOT NULL DEFAULT 1;

ALTER TABLE sessions ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN session_family_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN refresh_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN access_expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN refresh_expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN device_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN last_used_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN rotated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN replaced_by TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN revoked_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE product_branch_inventory ADD COLUMN freshness_status TEXT NOT NULL DEFAULT 'fresh';
ALTER TABLE product_branch_inventory ADD COLUMN stale_enforcement TEXT NOT NULL DEFAULT '';

CREATE TABLE auth_login_buckets(
 scope_kind TEXT NOT NULL, scope_hash TEXT NOT NULL,
 failed_attempts INTEGER NOT NULL DEFAULT 0,
 window_started_at TEXT NOT NULL, last_failed_at TEXT NOT NULL DEFAULT '',
 locked_until TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 PRIMARY KEY(scope_kind,scope_hash));

CREATE TABLE role_permissions(
 role TEXT NOT NULL, permission TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(role,permission));

CREATE TABLE supplier_members(
 supplier_id TEXT NOT NULL REFERENCES suppliers(id),
 account_id TEXT NOT NULL REFERENCES accounts(id),
 role TEXT NOT NULL DEFAULT 'supplier_advertiser',
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
 PRIMARY KEY(supplier_id,account_id),
 CHECK(role='supplier_advertiser'));

CREATE TABLE private_media_objects(
 id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
 purpose TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE,
 mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
 sha256_hex TEXT NOT NULL, original_name TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active', created_by TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(owner_kind IN('account','merchant','merchant_application','supplier','support_case')),
 CHECK(status IN('active','quarantined','archived')),
 CHECK(byte_size>=0));

CREATE TABLE private_media_access_grants(
 media_id TEXT NOT NULL REFERENCES private_media_objects(id),
 grantee_kind TEXT NOT NULL, grantee_id TEXT NOT NULL,
 permission TEXT NOT NULL DEFAULT 'read', expires_at TEXT NOT NULL DEFAULT '',
 granted_by TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(media_id,grantee_kind,grantee_id,permission));

CREATE TABLE security_audit_events(
 id TEXT PRIMARY KEY, event_kind TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT '',
 subject_kind TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '',
 context_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);

CREATE UNIQUE INDEX idx_session_id_unique ON sessions(session_id) WHERE session_id<>'';
CREATE UNIQUE INDEX idx_session_refresh_unique ON sessions(refresh_hash) WHERE refresh_hash<>'';
CREATE INDEX idx_session_account_scope ON sessions(account_id,active_role,merchant_id,revoked_at);
CREATE INDEX idx_session_family ON sessions(session_family_id,revoked_at);
CREATE INDEX idx_login_bucket_locked ON auth_login_buckets(locked_until,updated_at);
CREATE INDEX idx_supplier_member_account ON supplier_members(account_id,status);
CREATE INDEX idx_private_media_owner ON private_media_objects(owner_kind,owner_id,status);
CREATE INDEX idx_private_media_grantee ON private_media_access_grants(grantee_kind,grantee_id,expires_at);
CREATE INDEX idx_security_audit_time ON security_audit_events(event_kind,created_at);

INSERT OR IGNORE INTO role_permissions(role,permission,created_at) VALUES
 ('shopper','catalog.read',CURRENT_TIMESTAMP),
 ('shopper','cart.manage',CURRENT_TIMESTAMP),
 ('shopper','order.create',CURRENT_TIMESTAMP),
 ('shopper','order.read.own',CURRENT_TIMESTAMP),
 ('shopper','favorite.manage',CURRENT_TIMESTAMP),
 ('shopper','product.report',CURRENT_TIMESTAMP),
 ('merchant_owner','merchant.read',CURRENT_TIMESTAMP),
 ('merchant_owner','catalog.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','inventory.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','bundle.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','order.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','branch.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','delivery.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','return_policy.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','analytics.basic',CURRENT_TIMESTAMP),
 ('merchant_owner','promotion.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','supplier_hub.read',CURRENT_TIMESTAMP),
 ('merchant_owner','team.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','subscription.manage',CURRENT_TIMESTAMP),
 ('merchant_owner','private_media.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','merchant.read',CURRENT_TIMESTAMP),
 ('merchant_manager','catalog.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','inventory.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','bundle.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','order.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','branch.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','delivery.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','return_policy.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','analytics.basic',CURRENT_TIMESTAMP),
 ('merchant_manager','promotion.manage',CURRENT_TIMESTAMP),
 ('merchant_manager','supplier_hub.read',CURRENT_TIMESTAMP),
 ('merchant_manager','private_media.manage',CURRENT_TIMESTAMP),
 ('merchant_staff','merchant.read',CURRENT_TIMESTAMP),
 ('merchant_staff','catalog.read',CURRENT_TIMESTAMP),
 ('merchant_staff','inventory.manage',CURRENT_TIMESTAMP),
 ('merchant_staff','bundle.read',CURRENT_TIMESTAMP),
 ('merchant_staff','order.manage',CURRENT_TIMESTAMP),
 ('merchant_staff','supplier_hub.read',CURRENT_TIMESTAMP),
 ('supplier_advertiser','supplier_campaign.manage',CURRENT_TIMESTAMP),
 ('supplier_advertiser','supplier_lead.read',CURRENT_TIMESTAMP),
 ('supplier_advertiser','private_media.manage',CURRENT_TIMESTAMP),
 ('support_admin','support_case.manage',CURRENT_TIMESTAMP),
 ('support_admin','merchant.read',CURRENT_TIMESTAMP),
 ('support_admin','order.read.support',CURRENT_TIMESTAMP),
 ('support_admin','product_report.manage',CURRENT_TIMESTAMP),
 ('support_admin','private_media.read',CURRENT_TIMESTAMP),
 ('catalog_moderator','catalog.moderate',CURRENT_TIMESTAMP),
 ('catalog_moderator','product_report.manage',CURRENT_TIMESTAMP),
 ('merchant_reviewer','merchant_application.review',CURRENT_TIMESTAMP),
 ('merchant_reviewer','merchant.read',CURRENT_TIMESTAMP),
 ('merchant_reviewer','private_media.read',CURRENT_TIMESTAMP),
 ('finance','finance.read',CURRENT_TIMESTAMP),
 ('finance','subscription.manage',CURRENT_TIMESTAMP),
 ('advertising_manager','ad.manage',CURRENT_TIMESTAMP),
 ('advertising_manager','supplier.manage',CURRENT_TIMESTAMP),
 ('advertising_manager','supplier_campaign.review',CURRENT_TIMESTAMP),
 ('admin','admin.overview',CURRENT_TIMESTAMP),
 ('admin','merchant_application.review',CURRENT_TIMESTAMP),
 ('admin','merchant.manage',CURRENT_TIMESTAMP),
 ('admin','catalog.moderate',CURRENT_TIMESTAMP),
 ('admin','location.manage',CURRENT_TIMESTAMP),
 ('admin','plan.manage',CURRENT_TIMESTAMP),
 ('admin','ad.manage',CURRENT_TIMESTAMP),
 ('admin','supplier.manage',CURRENT_TIMESTAMP),
 ('admin','supplier_campaign.review',CURRENT_TIMESTAMP),
 ('admin','support_case.manage',CURRENT_TIMESTAMP),
 ('admin','product_report.manage',CURRENT_TIMESTAMP),
 ('admin','private_media.read',CURRENT_TIMESTAMP),
 ('admin','private_media.manage',CURRENT_TIMESTAMP),
 ('admin','audit.read',CURRENT_TIMESTAMP),
 ('super_admin','*',CURRENT_TIMESTAMP);
