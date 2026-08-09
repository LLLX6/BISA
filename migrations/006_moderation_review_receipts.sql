-- Short-lived, actor-bound receipts prove that a moderator opened the exact
-- product or merchant-ad state before approving or rejecting it.
CREATE TABLE moderation_review_receipts(
 receipt_hash TEXT PRIMARY KEY,
 target_kind TEXT NOT NULL CHECK(target_kind IN('product','ad')),
 target_id TEXT NOT NULL,
 reviewer_account_id TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL,
 issued_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 consumed_at TEXT NOT NULL DEFAULT '',
 decision TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_moderation_receipt_target
ON moderation_review_receipts(target_kind,target_id,reviewer_account_id,expires_at);
