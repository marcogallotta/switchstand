CREATE TABLE IF NOT EXISTS github_operations (
  operation_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'complete')),
  mutation_started INTEGER NOT NULL DEFAULT 0,
  commit_sha TEXT,
  result_json TEXT,
  lease_until INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
