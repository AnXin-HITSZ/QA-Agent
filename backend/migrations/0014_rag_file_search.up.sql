-- File search release pointer and persistent owner-scoped pagination.
CREATE TABLE rag_search_releases (
  id VARCHAR(128) NOT NULL,
  es_index VARCHAR(128) NOT NULL,
  files JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE rag_search_state (
  id VARCHAR(128) NOT NULL,
  active VARCHAR(128) NULL,
  previous VARCHAR(128) NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE rag_file_search_sessions (
  id VARCHAR(32) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  release_id VARCHAR(128) NOT NULL,
  metadata_json JSON NOT NULL,
  ready BOOL NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_rag_search_user (user_id),
  KEY ix_rag_search_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE rag_file_search_items (
  session_id VARCHAR(32) NOT NULL,
  ordinal INT NOT NULL,
  item JSON NOT NULL,
  PRIMARY KEY (session_id, ordinal)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
