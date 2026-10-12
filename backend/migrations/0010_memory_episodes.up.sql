-- Optional source evidence, explicitly enabled; application startup never creates it.
CREATE TABLE memory_episodes (
 id CHAR(32) NOT NULL,
 user_id CHAR(36) NOT NULL,
 scope VARCHAR(64) NOT NULL,
 turn_id VARCHAR(160) NOT NULL,
 generation INT NOT NULL,
 messages JSON NOT NULL,
 content_hash CHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 created_at DATETIME(6) NOT NULL,
 expires_at DATETIME(6) NOT NULL,
 PRIMARY KEY (id),
 UNIQUE KEY uk_memory_episodes_turn (user_id, scope, turn_id, generation),
 KEY ix_memory_episodes_owner (user_id, scope, status),
 KEY ix_memory_episodes_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
