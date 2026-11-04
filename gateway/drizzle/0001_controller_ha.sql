CREATE TABLE controller_config (id INTEGER PRIMARY KEY CHECK(id=1), site TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, heartbeat_seconds INTEGER NOT NULL DEFAULT 12, overflow_seconds INTEGER NOT NULL DEFAULT 2);
--> statement-breakpoint
CREATE TABLE controller_nodes (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, priority INTEGER NOT NULL, capacity INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, seen REAL NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0, load REAL NOT NULL DEFAULT 0, host TEXT NOT NULL DEFAULT '');
--> statement-breakpoint
CREATE TABLE controller_journal (site TEXT NOT NULL, seq INTEGER NOT NULL, hash TEXT NOT NULL, entry TEXT NOT NULL, PRIMARY KEY(site,seq));
--> statement-breakpoint
CREATE TABLE controller_events (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT, version INTEGER NOT NULL DEFAULT 0, owner TEXT, fence INTEGER NOT NULL DEFAULT 0, until REAL NOT NULL DEFAULT 0);
--> statement-breakpoint
INSERT INTO controller_events(id) VALUES(1);
--> statement-breakpoint
ALTER TABLE bridge_jobs ADD COLUMN controller TEXT;
