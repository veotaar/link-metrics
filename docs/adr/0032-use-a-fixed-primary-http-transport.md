# Use a fixed primary HTTP transport

Primary Trials use cleartext HTTP/1.1 over the private container network with 256 persistent keep-alive connections and no pipelining, response compression, TLS, or reverse proxy. Transport security, compression, and proxy behavior may be evaluated only in separate scenario families so their implementation differences do not contaminate application request handling.
