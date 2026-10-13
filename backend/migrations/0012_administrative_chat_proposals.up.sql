-- Persisted chat operations; explicit user confirmation required.
CREATE TABLE admin_matter_proposals (
	id VARCHAR(32) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	thread_id VARCHAR(160) NOT NULL, 
	matter_id VARCHAR(32) NOT NULL, 
	operation VARCHAR(32) NOT NULL, 
	payload JSON NOT NULL, 
	before_snapshot JSON NOT NULL, 
	dedupe_key VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	result JSON NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	expires_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_admin_proposal_dedupe UNIQUE (user_id, dedupe_key),
	KEY ix_admin_proposals_thread (user_id, thread_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
