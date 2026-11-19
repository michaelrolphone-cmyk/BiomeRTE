CREATE TABLE bridge_result_chunks (
 job TEXT NOT NULL REFERENCES bridge_jobs(id) ON DELETE CASCADE,
 lease TEXT NOT NULL, digest TEXT NOT NULL, part INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(job, lease, digest, part)
);
