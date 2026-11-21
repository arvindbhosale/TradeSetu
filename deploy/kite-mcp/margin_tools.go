package mcp

// Added by the desk's Dockerfile to Zerodha's kite-mcp-server: Kite Connect's basket margin calculator
// (POST /margins/basket), which the upstream server does not expose. It returns Zerodha's own SPAN, exposure
// and premium figures for a set of orders, before ("initial") and after ("final") the hedge benefit.

import (
	"context"
	"fmt"
	"strings"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
	kiteconnect "github.com/zerodha/gokiteconnect/v4"
	"github.com/zerodha/kite-mcp-server/kc"
)

const maxBasketOrders = 50

type DeskBasketMarginsTool struct{}

func (*DeskBasketMarginsTool) Tool() mcp.Tool {
	return mcp.NewTool("get_basket_margins",
		mcp.WithDescription("Margin Zerodha requires for a basket of orders (SPAN, exposure, option premium), "+
			"with the hedge benefit: 'initial' is without it, 'final' with it, 'orders' per order. Read-only: nothing is placed."),
		mcp.WithArray("orders",
			mcp.Description("Orders: exchange, tradingsymbol, transaction_type (BUY/SELL), quantity, and optionally "+
				"product (NRML, MIS, CNC; default NRML), order_type (default MARKET), price, trigger_price, variety (default regular)."),
			mcp.Required(),
			mcp.Items(map[string]any{"type": "object"}),
		),
		mcp.WithBoolean("consider_positions",
			mcp.Description("Take the account's open positions into account. Default false."),
		),
	)
}

func (*DeskBasketMarginsTool) Handler(manager *kc.Manager) server.ToolHandlerFunc {
	handler := NewToolHandler(manager)
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		handler.trackToolCall(ctx, "get_basket_margins")
		args := request.GetArguments()
		raw, ok := args["orders"].([]interface{})
		if !ok || len(raw) == 0 {
			return mcp.NewToolResultError("orders must be a non-empty list"), nil
		}
		if len(raw) > maxBasketOrders {
			return mcp.NewToolResultError(fmt.Sprintf("at most %d orders at a time", maxBasketOrders)), nil
		}
		params := make([]kiteconnect.OrderMarginParam, 0, len(raw))
		for n, item := range raw {
			o, ok := item.(map[string]interface{})
			if !ok {
				return mcp.NewToolResultError(fmt.Sprintf("order %d is not an object", n+1)), nil
			}
			p := kiteconnect.OrderMarginParam{
				Exchange:        strings.ToUpper(SafeAssertString(o["exchange"], "")),
				Tradingsymbol:   strings.ToUpper(SafeAssertString(o["tradingsymbol"], "")),
				TransactionType: strings.ToUpper(SafeAssertString(o["transaction_type"], "")),
				Variety:         SafeAssertString(o["variety"], "regular"),
				Product:         strings.ToUpper(SafeAssertString(o["product"], "NRML")),
				OrderType:       strings.ToUpper(SafeAssertString(o["order_type"], "MARKET")),
				Quantity:        SafeAssertFloat64(o["quantity"], 0),
				Price:           SafeAssertFloat64(o["price"], 0),
				TriggerPrice:    SafeAssertFloat64(o["trigger_price"], 0),
			}
			if p.Exchange == "" || p.Tradingsymbol == "" || p.Quantity <= 0 ||
				(p.TransactionType != "BUY" && p.TransactionType != "SELL") {
				return mcp.NewToolResultError(fmt.Sprintf(
					"order %d needs exchange, tradingsymbol, transaction_type BUY or SELL and a positive quantity", n+1)), nil
			}
			params = append(params, p)
		}
		consider := SafeAssertBool(args["consider_positions"], false)
		return handler.WithSession(ctx, "get_basket_margins", func(session *kc.KiteSessionData) (*mcp.CallToolResult, error) {
			out, err := session.Kite.Client.GetBasketMargins(kiteconnect.GetBasketParams{
				OrderParams: params, ConsiderPositions: consider,
			})
			if err != nil {
				handler.trackToolError(ctx, "get_basket_margins", "api_error")
				return mcp.NewToolResultError("Failed to calculate margins: " + err.Error()), nil
			}
			return handler.MarshalResponse(out, "get_basket_margins")
		})
	}
}
