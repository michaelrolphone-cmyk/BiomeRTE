CREATE TABLE `biome_owner` (
	`singleton` integer PRIMARY KEY NOT NULL,
	`user_id` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `biome_oauth_clients` (
	`id` text PRIMARY KEY NOT NULL,
	`metadata` text NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `biome_oauth_clients_expires` ON `biome_oauth_clients` (`expires`);--> statement-breakpoint
CREATE TABLE `biome_oauth_codes` (
	`hash` text PRIMARY KEY NOT NULL,
	`principal` text NOT NULL,
	`request` text NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `biome_oauth_codes_expires` ON `biome_oauth_codes` (`expires`);--> statement-breakpoint
CREATE TABLE `biome_oauth_pending` (
	`hash` text PRIMARY KEY NOT NULL,
	`principal` text NOT NULL,
	`request` text NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `biome_oauth_pending_expires` ON `biome_oauth_pending` (`expires`);--> statement-breakpoint
CREATE TABLE `biome_oauth_tokens` (
	`hash` text PRIMARY KEY NOT NULL,
	`kind` text NOT NULL,
	`grant_id` text NOT NULL,
	`client_id` text NOT NULL,
	`principal` text NOT NULL,
	`resource` text NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `biome_oauth_token_grant` ON `biome_oauth_tokens` (`grant_id`);--> statement-breakpoint
CREATE INDEX `biome_oauth_token_client` ON `biome_oauth_tokens` (`client_id`);--> statement-breakpoint
CREATE INDEX `biome_oauth_token_expires` ON `biome_oauth_tokens` (`expires`);
--> statement-breakpoint
CREATE TABLE bridge_jobs (
 id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 deadline INTEGER NOT NULL, lease_until INTEGER, lease TEXT, result TEXT, updated INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX bridge_dispatch ON bridge_jobs(state, deadline, lease_until);
--> statement-breakpoint
CREATE INDEX bridge_cleanup ON bridge_jobs(updated);
