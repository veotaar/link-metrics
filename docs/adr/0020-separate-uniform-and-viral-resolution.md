# Separate uniform and viral resolution scenarios

Short Link resolution is measured with two separately reported access distributions: `uniform`, which selects evenly across all 1,000,000 seeded Short Links, and `viral`, in which 90% of requests target one Short Code while 10% are uniform. Separating ordinary access from intentional row-lock contention prevents an arbitrary popularity mix from obscuring either behavior.
