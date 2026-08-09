-- Move legacy B2B notices out of the shared account namespace.  A supplier
-- actor is authorized against supplier_members, while the same account may
-- independently retain a shopper role.
UPDATE notifications
SET target_kind='supplier',
    target_id=(
      SELECT sc.supplier_id
      FROM supplier_campaigns sc
      WHERE sc.id=substr(notifications.route,length('supplier:campaign:')+1)
    )
WHERE target_kind='account'
  AND route LIKE 'supplier:campaign:%'
  AND EXISTS(
    SELECT 1 FROM supplier_campaigns sc
    WHERE sc.id=substr(notifications.route,length('supplier:campaign:')+1)
  );
