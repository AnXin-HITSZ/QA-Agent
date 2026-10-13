-- Structural rollback only: deleted business records/audit are NOT recoverable.
-- 0011 Administrative transactions; apply manually, never at startup.
CREATE TABLE admin_matter_events (
	id VARCHAR(32) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	matter_id VARCHAR(32) NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	description TEXT NOT NULL, 
	source_type VARCHAR(32) NOT NULL, 
	source_ref VARCHAR(500) NOT NULL, 
	before_snapshot JSON NOT NULL, 
	after_snapshot JSON NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_admin_event_request UNIQUE (user_id, request_id),
	KEY ix_admin_events_matter (matter_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE admin_matter_links (
	id VARCHAR(32) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	matter_id VARCHAR(32) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	details JSON NOT NULL, 
	revision INTEGER NOT NULL, 
	deleted INTEGER NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id),
	KEY ix_admin_links_matter (matter_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE admin_matter_projections (
	id VARCHAR(32) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	published_revision INTEGER NOT NULL, 
	attempts INTEGER NOT NULL, 
	last_error VARCHAR(100) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE admin_matters (
	id VARCHAR(32) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	revision INTEGER NOT NULL, 
	details JSON NOT NULL, 
	deleted INTEGER NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id),
	KEY ix_admin_matters_owner (user_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
