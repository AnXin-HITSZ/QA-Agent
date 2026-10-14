CREATE TABLE rag_index_jobs (
  namespace VARCHAR(64) NOT NULL,
  id VARCHAR(64) NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  status VARCHAR(16) NOT NULL,
  data JSON NOT NULL,
  PRIMARY KEY (namespace, id),
  KEY ix_rag_index_jobs_namespace_order (namespace, created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE rag_index_job_files (
  namespace VARCHAR(64) NOT NULL,
  job_id VARCHAR(64) NOT NULL,
  ordinal INT NOT NULL,
  data JSON NOT NULL,
  PRIMARY KEY (namespace, job_id, ordinal)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE rag_index_version_files (
  namespace VARCHAR(64) NOT NULL,
  version VARCHAR(128) NOT NULL,
  key_hash VARCHAR(64) NOT NULL,
  data JSON NOT NULL,
  PRIMARY KEY (namespace, version, key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
