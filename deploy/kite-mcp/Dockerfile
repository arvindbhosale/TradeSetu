# Zerodha's kite-mcp-server, built from source. It is what places live orders:
# the hosted mcp.kite.trade is read-only and needs a browser on the same machine.
FROM golang:1.24 AS builder
ARG KITE_MCP_REF=master
RUN git clone --depth 1 --branch ${KITE_MCP_REF} https://github.com/zerodha/kite-mcp-server /src
WORKDIR /src
# The desk's addition: Kite Connect's basket margin calculator as the get_basket_margins tool (exact SPAN,
# exposure and hedge benefit). Skipped if upstream ever ships a tool of that name; the build fails loudly if
# the tool list moved, rather than shipping a server without it.
COPY margin_tools.go /tmp/margin_tools.go
RUN if grep -rq '"get_basket_margins"' mcp; then echo "upstream has get_basket_margins"; else \
      cp /tmp/margin_tools.go mcp/margin_tools.go && \
      sed -i 's/^\(\s*\)&MarginsTool{},/&\n\1\&DeskBasketMarginsTool{},/' mcp/mcp.go && \
      grep -q 'DeskBasketMarginsTool{}' mcp/mcp.go; fi
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /kite-mcp-server .

# Static binary, CA certificates and time zones included, no shell, non-root.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /kite-mcp-server /kite-mcp-server
ENV APP_MODE=http APP_HOST=0.0.0.0 APP_PORT=8080
EXPOSE 8080
ENTRYPOINT ["/kite-mcp-server"]
