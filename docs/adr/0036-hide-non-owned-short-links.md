# Hide non-owned Short Links

Statistics lookup returns the same `404 Not Found` response when a Short Code is missing or belongs to another User, replacing the previous `403 Forbidden` distinction. This treats Short Link ownership as an information boundary, sacrificing a more diagnostic response to avoid exposing another User's Short Link existence.
