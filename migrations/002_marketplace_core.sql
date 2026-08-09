-- Additive BISA marketplace core. No legacy table or column is removed.

ALTER TABLE schema_migrations ADD COLUMN checksum TEXT NOT NULL DEFAULT '';

ALTER TABLE accounts ADD COLUMN preferred_language TEXT NOT NULL DEFAULT 'ar';
ALTER TABLE accounts ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';

ALTER TABLE product_categories ADD COLUMN slug TEXT NOT NULL DEFAULT '';
ALTER TABLE product_categories ADD COLUMN description_ar TEXT NOT NULL DEFAULT '';
ALTER TABLE product_categories ADD COLUMN description_en TEXT NOT NULL DEFAULT '';

ALTER TABLE store_branches ADD COLUMN phone TEXT NOT NULL DEFAULT '';
ALTER TABLE store_branches ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Muscat';
ALTER TABLE store_branches ADD COLUMN last_open_status_at TEXT NOT NULL DEFAULT '';

ALTER TABLE products ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE products ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE products ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'approved';
ALTER TABLE products ADD COLUMN archived_at TEXT NOT NULL DEFAULT '';

ALTER TABLE bundles ADD COLUMN image_path TEXT NOT NULL DEFAULT '';
ALTER TABLE bundles ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE bundles ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'approved';

ALTER TABLE orders ADD COLUMN expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'unavailable';
ALTER TABLE orders ADD COLUMN cancellation_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE notifications ADD COLUMN seen_at TEXT NOT NULL DEFAULT '';
ALTER TABLE notifications ADD COLUMN acknowledged_at TEXT NOT NULL DEFAULT '';
ALTER TABLE notifications ADD COLUMN dismissed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE notifications ADD COLUMN expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE notifications ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

CREATE TABLE shopper_profiles(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),
 display_name TEXT NOT NULL DEFAULT '', default_address_id TEXT NOT NULL DEFAULT '',
 marketing_opt_in INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE merchant_members(
 merchant_id TEXT NOT NULL REFERENCES merchants(id), account_id TEXT NOT NULL REFERENCES accounts(id),
 role TEXT NOT NULL CHECK(role IN('merchant_owner','merchant_manager','merchant_staff')),
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
 PRIMARY KEY(merchant_id,account_id));

CREATE TABLE merchant_application_steps(
 application_id TEXT NOT NULL REFERENCES merchant_applications(id), step_key TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}', completed_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 PRIMARY KEY(application_id,step_key));

CREATE TABLE branch_delivery_zones(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES store_branches(id), mode TEXT NOT NULL,
 wilayah_id TEXT NOT NULL DEFAULT '', area_id TEXT NOT NULL DEFAULT '',
 fee_baisa INTEGER NOT NULL DEFAULT 0, minimum_baisa INTEGER NOT NULL DEFAULT 0,
 free_threshold_baisa INTEGER NOT NULL DEFAULT 0, eta_text TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(mode IN('office_delivery','home_delivery')));

CREATE TABLE product_media(
 id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id), private_path TEXT NOT NULL,
 thumbnail_path TEXT NOT NULL DEFAULT '', mime_type TEXT NOT NULL, width INTEGER NOT NULL DEFAULT 0,
 height INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);

CREATE TABLE inventory_audit_events(
 id TEXT PRIMARY KEY, audit_id TEXT NOT NULL REFERENCES inventory_audits(id),
 product_id TEXT NOT NULL REFERENCES products(id), event_type TEXT NOT NULL,
 before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}',
 actor_id TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE shopper_addresses(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), address_type TEXT NOT NULL,
 label TEXT NOT NULL DEFAULT '', governorate_id TEXT NOT NULL DEFAULT '', wilayah_id TEXT NOT NULL,
 area_id TEXT NOT NULL, address_text TEXT NOT NULL, latitude REAL, longitude REAL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(address_type IN('home','office','other')));

CREATE TABLE order_events(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), event_type TEXT NOT NULL,
 from_status TEXT NOT NULL DEFAULT '', to_status TEXT NOT NULL DEFAULT '', actor_kind TEXT NOT NULL,
 actor_id TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);

CREATE TABLE order_policy_snapshots(
 order_id TEXT PRIMARY KEY REFERENCES orders(id), policy_id TEXT NOT NULL DEFAULT '',
 policy_version INTEGER NOT NULL DEFAULT 0, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE recent_views(
 account_id TEXT NOT NULL REFERENCES accounts(id), entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 last_viewed_at TEXT NOT NULL, view_count INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(account_id,entity_kind,entity_id));

CREATE TABLE search_events(
 id TEXT PRIMARY KEY, actor_hash TEXT NOT NULL DEFAULT '', query_normalized TEXT NOT NULL,
 result_count INTEGER NOT NULL DEFAULT 0, wilayah_id TEXT NOT NULL DEFAULT '', area_id TEXT NOT NULL DEFAULT '',
 filters_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);

CREATE TABLE product_reports(
 id TEXT PRIMARY KEY, reporter_account_id TEXT NOT NULL REFERENCES accounts(id),
 product_id TEXT NOT NULL REFERENCES products(id), reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open', reviewed_by TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE feature_flags(
 key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, rollout_percent INTEGER NOT NULL DEFAULT 0,
 audiences_json TEXT NOT NULL DEFAULT '[]', config_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 CHECK(rollout_percent BETWEEN 0 AND 100));

CREATE TABLE admin_role_permissions(
 role TEXT NOT NULL, permission TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(role,permission));

CREATE TABLE account_permission_overrides(
 account_id TEXT NOT NULL REFERENCES accounts(id), permission TEXT NOT NULL, allowed INTEGER NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(account_id,permission));

CREATE TABLE notification_templates(
 key TEXT PRIMARY KEY, title_ar TEXT NOT NULL, title_en TEXT NOT NULL,
 body_ar TEXT NOT NULL, body_en TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL);

CREATE TABLE external_action_attempts(
 id TEXT PRIMARY KEY, adapter TEXT NOT NULL, action_kind TEXT NOT NULL, target_kind TEXT NOT NULL,
 target_id TEXT NOT NULL, status TEXT NOT NULL, provider_reference TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE idempotency_records(
 actor_id TEXT NOT NULL, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 payload_hash TEXT NOT NULL, response_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 PRIMARY KEY(actor_id,operation,idempotency_key));

CREATE TABLE ad_placements(
 key TEXT PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
 max_active INTEGER NOT NULL DEFAULT 1, frequency_cap INTEGER NOT NULL DEFAULT 3,
 enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);

CREATE TABLE merchant_campaign_credits(
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id), source_kind TEXT NOT NULL,
 source_id TEXT NOT NULL DEFAULT '', placement TEXT NOT NULL, remaining_uses INTEGER NOT NULL,
 expires_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);

CREATE TABLE support_cases(
 id TEXT PRIMARY KEY, opened_by TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
 category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'normal',
 assigned_to TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE payment_attempts(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), adapter TEXT NOT NULL,
 amount_baisa INTEGER NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 provider_reference TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(order_id,idempotency_key));

CREATE INDEX idx_public_branch_location ON store_branches(wilayah_id,area_id,status,active,public_visible);
CREATE INDEX idx_product_discovery ON products(category_id,status,active,moderation_status,created_at);
CREATE INDEX idx_inventory_discovery ON product_branch_inventory(branch_id,availability,active,last_stock_verified_at);
CREATE INDEX idx_order_merchant_status ON orders(merchant_id,status,created_at);
CREATE INDEX idx_order_account_status ON orders(account_id,status,created_at);
CREATE INDEX idx_order_events_timeline ON order_events(order_id,created_at,id);
CREATE INDEX idx_analytics_merchant ON analytics_events(entity_kind,entity_id,event_type,created_at);
CREATE INDEX idx_search_events_time ON search_events(created_at,result_count);
CREATE INDEX idx_product_reports_queue ON product_reports(status,created_at);
CREATE INDEX idx_notification_pending ON notifications(target_kind,target_id,requires_action,acted_at,expires_at,priority);
CREATE INDEX idx_delivery_zone_lookup ON branch_delivery_zones(branch_id,mode,wilayah_id,area_id,active);
