# Prohibit application caching in primary scenarios

Primary Scenarios require every measured request to perform its prescribed PostgreSQL interaction without caching Users, password hashes, tokens, Short Links, destinations, or statistics and without batching work across requests. Cache-enabled architectures may be introduced only as a separately reported future benchmark family, keeping the baseline focused on equivalent backend-plus-database paths.
