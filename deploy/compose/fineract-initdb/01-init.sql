-- Creates the two databases Fineract Core expects (mirrors the official
-- apache/fineract postgres init). Runs once on first DB container start.
-- Using .sql (not .sh) avoids CRLF/shebang issues on Windows bind mounts.
CREATE DATABASE fineract_tenants;
CREATE DATABASE fineract_default;
