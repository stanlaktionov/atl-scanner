"""Disk cache of raw API responses.

Layout: <raw_dir>/<source>/<endpoint>/<YYYY-MM-DD>/<request-hash>.json. Files are immutable;
TTL per endpoint comes from config."""
