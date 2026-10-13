-- Stop all legacy administrative writers before running this destructive migration.
-- Deletes business records and audit in these five retired tables only.
DROP TABLE IF EXISTS admin_matter_proposals;
DROP TABLE IF EXISTS admin_matter_projections;
DROP TABLE IF EXISTS admin_matter_links;
DROP TABLE IF EXISTS admin_matter_events;
DROP TABLE IF EXISTS admin_matters;
