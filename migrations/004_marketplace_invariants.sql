-- Additive defense-in-depth constraints for tenant and commerce invariants.
-- Application services still validate first so clients receive stable errors.

ALTER TABLE merchant_documents ADD COLUMN media_id TEXT NOT NULL DEFAULT '';
ALTER TABLE merchant_documents ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE merchant_documents ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT '';
ALTER TABLE merchant_documents ADD COLUMN reviewed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE merchant_documents ADD COLUMN review_note TEXT NOT NULL DEFAULT '';

UPDATE merchant_documents
SET media_id=substr(private_path,7)
WHERE media_id='' AND private_path LIKE 'media:%';

CREATE INDEX idx_merchant_document_review
ON merchant_documents(application_id,review_status,kind);

CREATE UNIQUE INDEX idx_active_merchant_subscription
ON merchant_subscriptions(merchant_id)
WHERE status IN ('active','pending_payment','grace');

CREATE UNIQUE INDEX idx_active_return_policy
ON merchant_return_policies(merchant_id)
WHERE active=1;

CREATE UNIQUE INDEX idx_active_branch_delivery_zone
ON branch_delivery_zones(branch_id,mode,wilayah_id,area_id)
WHERE active=1;

CREATE INDEX idx_product_inventory_freshness
ON product_branch_inventory(branch_id,active,freshness_status,stale_at);

CREATE INDEX idx_order_response_queue
ON orders(status,response_due_at,expires_at);

CREATE INDEX idx_supplier_campaign_window
ON supplier_campaigns(status,starts_at,ends_at);

CREATE TRIGGER product_inventory_same_merchant_insert
BEFORE INSERT ON product_branch_inventory
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM products p JOIN store_branches b ON b.id=NEW.branch_id
    WHERE p.id=NEW.product_id AND p.merchant_id=b.merchant_id
  ) THEN RAISE(ABORT,'product_branch_tenant_mismatch') END;
END;

CREATE TRIGGER product_inventory_same_merchant_update
BEFORE UPDATE OF product_id,branch_id ON product_branch_inventory
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM products p JOIN store_branches b ON b.id=NEW.branch_id
    WHERE p.id=NEW.product_id AND p.merchant_id=b.merchant_id
  ) THEN RAISE(ABORT,'product_branch_tenant_mismatch') END;
END;

CREATE TRIGGER bundle_branch_same_merchant_insert
BEFORE INSERT ON bundles
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM store_branches b WHERE b.id=NEW.branch_id AND b.merchant_id=NEW.merchant_id
  ) THEN RAISE(ABORT,'bundle_branch_tenant_mismatch') END;
END;

CREATE TRIGGER bundle_branch_same_merchant_update
BEFORE UPDATE OF merchant_id,branch_id ON bundles
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM store_branches b WHERE b.id=NEW.branch_id AND b.merchant_id=NEW.merchant_id
  ) THEN RAISE(ABORT,'bundle_branch_tenant_mismatch') END;
END;

CREATE TRIGGER bundle_item_same_merchant_insert
BEFORE INSERT ON bundle_items
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM bundles bu
    JOIN products p ON p.id=NEW.product_id AND p.merchant_id=bu.merchant_id
    JOIN product_branch_inventory i ON i.product_id=p.id AND i.branch_id=bu.branch_id
    WHERE bu.id=NEW.bundle_id
  ) THEN RAISE(ABORT,'bundle_component_tenant_mismatch') END;
END;

CREATE TRIGGER bundle_item_same_merchant_update
BEFORE UPDATE OF bundle_id,product_id ON bundle_items
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM bundles bu
    JOIN products p ON p.id=NEW.product_id AND p.merchant_id=bu.merchant_id
    JOIN product_branch_inventory i ON i.product_id=p.id AND i.branch_id=bu.branch_id
    WHERE bu.id=NEW.bundle_id
  ) THEN RAISE(ABORT,'bundle_component_tenant_mismatch') END;
END;

CREATE TRIGGER cart_branch_same_merchant_insert
BEFORE INSERT ON carts
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM store_branches b WHERE b.id=NEW.branch_id AND b.merchant_id=NEW.merchant_id
  ) THEN RAISE(ABORT,'cart_branch_tenant_mismatch') END;
END;

CREATE TRIGGER cart_branch_same_merchant_update
BEFORE UPDATE OF merchant_id,branch_id ON carts
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM store_branches b WHERE b.id=NEW.branch_id AND b.merchant_id=NEW.merchant_id
  ) THEN RAISE(ABORT,'cart_branch_tenant_mismatch') END;
END;

CREATE TRIGGER cart_item_scope_insert
BEFORE INSERT ON cart_items
BEGIN
  SELECT CASE
    WHEN NEW.item_kind='product' AND NOT EXISTS(
      SELECT 1 FROM carts c JOIN products p ON p.id=NEW.item_id AND p.merchant_id=c.merchant_id
      JOIN product_branch_inventory i ON i.product_id=p.id AND i.branch_id=c.branch_id
      WHERE c.id=NEW.cart_id
    ) THEN RAISE(ABORT,'cart_product_scope_mismatch')
    WHEN NEW.item_kind='bundle' AND NOT EXISTS(
      SELECT 1 FROM carts c JOIN bundles b ON b.id=NEW.item_id
       AND b.merchant_id=c.merchant_id AND b.branch_id=c.branch_id
      WHERE c.id=NEW.cart_id
    ) THEN RAISE(ABORT,'cart_bundle_scope_mismatch')
    WHEN NEW.item_kind NOT IN ('product','bundle') THEN RAISE(ABORT,'invalid_cart_item_kind')
  END;
END;

CREATE TRIGGER inventory_reservation_scope_insert
BEFORE INSERT ON inventory_reservations
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM orders o
    JOIN products p ON p.id=NEW.product_id AND p.merchant_id=o.merchant_id
    JOIN product_branch_inventory i ON i.product_id=p.id AND i.branch_id=o.branch_id
    WHERE o.id=NEW.order_id AND o.branch_id=NEW.branch_id
  ) THEN RAISE(ABORT,'reservation_scope_mismatch') END;
END;

CREATE TRIGGER delivery_zone_hierarchy_insert
BEFORE INSERT ON branch_delivery_zones
WHEN NEW.area_id<>''
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM locations a WHERE a.id=NEW.area_id AND a.kind='area'
      AND a.parent_id=NEW.wilayah_id AND a.active=1
  ) THEN RAISE(ABORT,'delivery_zone_hierarchy_mismatch') END;
END;

CREATE TRIGGER delivery_zone_threshold_insert
BEFORE INSERT ON branch_delivery_zones
BEGIN
  SELECT CASE WHEN NEW.free_threshold_baisa>0
    AND NEW.free_threshold_baisa<NEW.minimum_baisa
    THEN RAISE(ABORT,'free_threshold_below_minimum') END;
END;

CREATE TRIGGER delivery_zone_threshold_update
BEFORE UPDATE OF minimum_baisa,free_threshold_baisa ON branch_delivery_zones
BEGIN
  SELECT CASE WHEN NEW.free_threshold_baisa>0
    AND NEW.free_threshold_baisa<NEW.minimum_baisa
    THEN RAISE(ABORT,'free_threshold_below_minimum') END;
END;

CREATE TRIGGER fulfillment_threshold_insert
BEFORE INSERT ON fulfillment_profiles
BEGIN
  SELECT CASE
    WHEN NEW.office_free_threshold_baisa>0
      AND NEW.office_free_threshold_baisa<NEW.office_minimum_baisa
      THEN RAISE(ABORT,'office_free_threshold_below_minimum')
    WHEN NEW.home_free_threshold_baisa>0
      AND NEW.home_free_threshold_baisa<NEW.home_minimum_baisa
      THEN RAISE(ABORT,'home_free_threshold_below_minimum')
  END;
END;

CREATE TRIGGER fulfillment_threshold_update
BEFORE UPDATE OF office_minimum_baisa,office_free_threshold_baisa,
  home_minimum_baisa,home_free_threshold_baisa ON fulfillment_profiles
BEGIN
  SELECT CASE
    WHEN NEW.office_free_threshold_baisa>0
      AND NEW.office_free_threshold_baisa<NEW.office_minimum_baisa
      THEN RAISE(ABORT,'office_free_threshold_below_minimum')
    WHEN NEW.home_free_threshold_baisa>0
      AND NEW.home_free_threshold_baisa<NEW.home_minimum_baisa
      THEN RAISE(ABORT,'home_free_threshold_below_minimum')
  END;
END;
