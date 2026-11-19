-- Claimed requests may hold a live lease beyond their dispatch deadline.
-- Keep diagnostics on live ownership ranges rather than historical payloads.
CREATE INDEX bridge_live_claims ON bridge_jobs(lease_until) WHERE state='claimed';
